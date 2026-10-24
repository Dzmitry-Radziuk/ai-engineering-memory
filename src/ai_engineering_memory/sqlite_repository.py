"""SQLite-адаптер контракта репозитория знаний."""

from __future__ import annotations

from typing import Any

from ai_engineering_memory.storage import Storage


class SqliteKnowledgeRepository:
    """Реализует порт знаний через совместимый SQLite storage facade."""

    def __init__(self, store: Storage):
        """Принимает storage instance с явным путём базы."""
        self._store = store

    def search(
        self,
        query: str,
        *,
        namespace: str | None,
        status: str | None,
        category: str | None,
        limit: int,
        prompt_context: bool,
        read_only: bool,
    ) -> list[dict[str, Any]]:
        """Ищет знания через существующую SQLite/FTS реализацию."""
        return self._store.search_knowledge(
            query,
            namespace=namespace,
            status=status,
            category=category,
            limit=limit,
            prompt_context=prompt_context,
            _read_only=read_only,
        )

    def get(self, knowledge_id: int) -> dict[str, Any] | None:
        """Возвращает одну запись по стабильному integer ID."""
        result = self._store.get_knowledge(knowledge_id)
        return result if isinstance(result, dict) else None

    def get_evidence(self, knowledge_id: int) -> list[dict[str, Any]]:
        """Возвращает evidence через совместимый SQLite API."""
        return self._store.get_knowledge_evidence(knowledge_id)

    def get_corroboration(self, knowledge_id: int) -> dict[str, Any]:
        """Возвращает межпроектные подтверждения через SQLite API."""
        return self._store.get_learning_corroboration(knowledge_id)

    def list_knowledge(
        self,
        *,
        status: str | None,
        namespace: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Возвращает записи через совместимый SQLite storage API."""
        return self._store.list_knowledge(
            status=status, namespace=namespace, limit=limit,
        )

    def create_candidate(
        self,
        category: str,
        title: str,
        content: str,
        *,
        namespace: str,
        technology: str,
    ) -> dict[str, Any]:
        """Создаёт запись только через существующий candidate lifecycle."""
        return self._store.create_knowledge(
            category,
            title,
            content,
            namespace=namespace,
            technology=technology,
        )

    def list_candidates(
        self, *, namespace: str, limit: int,
    ) -> list[dict[str, Any]]:
        """Возвращает candidates из одного точного namespace."""
        return self._store.list_knowledge(
            status="candidate", namespace=namespace, limit=limit,
        )

    def overview(self, *, namespace: str) -> dict[str, Any]:
        """Возвращает счётчики знаний одного namespace."""
        return self._store.memory_overview(namespace=namespace)


class SqliteLearningRepository:
    """Адаптер очереди обучения к существующему SQLite storage API."""

    def __init__(self, store: Storage):
        """Принимает хранилище, привязанное к одной базе данных."""
        self._store = store

    def get_event_by_verification(
        self, verification_id: str,
    ) -> dict[str, Any] | None:
        return self._store.get_learning_event_by_verification(verification_id)

    def ingest_task_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Сохраняет ограниченное legacy learning-событие через SQLite facade."""
        return self._store.ingest_task_event(event)

    def get_task_status(self, event_id: str) -> dict[str, Any] | None:
        """Читает состояние события через SQLite facade."""
        return self._store.get_task_status(event_id)

    def status_summary(self) -> dict[str, Any]:
        return self._store.learning_status_summary()

    def list_events(self, *, limit: int) -> list[dict[str, Any]]:
        return self._store.list_learning_events(limit=limit)

    def list_attempts(
        self, *, limit: int, failed_only: bool,
    ) -> list[dict[str, Any]]:
        return self._store.list_verification_attempts(
            limit=limit, failed_only=failed_only,
        )

    def retry_event(self, event_id: str) -> None:
        self._store.retry_learning_event(event_id)

    def withdraw_candidate(
        self, knowledge_id: int, *, reason: str,
    ) -> dict[str, Any]:
        return self._store.withdraw_learning_candidate(
            knowledge_id, reason=reason,
        )


class LocalSkillsRepository:
    """Адаптер безопасных Skills filesystem и proposal API."""

    def __init__(self, store: Storage):
        """Принимает storage facade текущего запуска."""
        self._store = store

    def list_skills(self) -> list[dict[str, Any]]:
        return self._store.list_skills()

    def get_skill(self, name: str) -> dict[str, Any]:
        return self._store.get_skill(name)

    def propose_update(
        self, skill_name: str, proposer: str, description: str, content: str,
    ) -> str:
        return self._store.propose_skill_update(
            skill_name, proposer, description, content,
        )

    def list_proposals(self, status: str) -> list[dict[str, Any]]:
        return self._store.list_skill_proposals(status)

    def get_proposal(self, proposal_id: str) -> dict[str, Any]:
        return self._store.get_skill_proposal(proposal_id)

    def get_history(self, skill_name: str) -> list[dict[str, Any]]:
        return self._store.get_skill_history(skill_name)


class LocalAdminRepository:
    """Адаптер lifecycle decisions к существующему audit-capable storage API."""

    def __init__(self, store: Storage):
        """Принимает storage facade выбранной базы и Skills-каталога."""
        self._store = store

    def approve_knowledge(self, knowledge_id: int) -> dict[str, Any]:
        return self._store.approve_knowledge(knowledge_id)

    def reject_knowledge(
        self, knowledge_id: int, *, reason: str,
    ) -> dict[str, Any]:
        return self._store.reject_knowledge(knowledge_id, reason=reason)

    def deprecate_knowledge(self, knowledge_id: int) -> dict[str, Any]:
        return self._store.deprecate_knowledge(knowledge_id)

    def decide_skill_proposal(
        self, proposal_id: str, *, decision: str,
        maintainer: str, reason: str,
    ) -> dict[str, Any]:
        return self._store.decide_skill_proposal(
            proposal_id, decision=decision, maintainer=maintainer, reason=reason,
        )

    def rollback_skill_version(
        self, skill_name: str, version: str, *, maintainer: str,
    ) -> dict[str, Any]:
        return self._store.rollback_skill_version(
            skill_name, version, maintainer=maintainer,
        )
