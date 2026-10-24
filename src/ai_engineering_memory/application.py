"""Прикладные сценарии работы с памятью."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ai_engineering_memory.plugin_api import SearchBackend, SearchHit, SearchQuery
from ai_engineering_memory.ports import (
    AdminRepository,
    KnowledgeRepository,
    LearningAnalysisProvider,
    LearningRepository,
    LearningWorkflow,
    SkillsRepository,
)


class MemoryService:
    """Оркестрирует пользовательские сценарии поверх порта знаний."""

    def __init__(
        self,
        knowledge: KnowledgeRepository,
        *,
        search_backend: SearchBackend | None = None,
    ):
        """Принимает репозиторий и необязательный явно выбранный search backend."""
        self._knowledge = knowledge
        self._search_backend = search_backend

    def search(
        self,
        query: str,
        *,
        namespace: str | None,
        status: str | None = "approved",
        category: str | None = None,
        limit: int = 10,
        prompt_context: bool = False,
        read_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Ищет знания с проверкой фильтров ядром и default approved."""
        if self._search_backend is not None:
            self._validate_plugin_search(
                query,
                namespace=namespace,
                status=status,
                category=category,
                limit=limit,
                prompt_context=prompt_context,
            )
            query = query.strip()
            namespace = namespace.strip() if namespace is not None else None
            status = status.strip() if status is not None else None
            category = category.strip() if category is not None else None
            hits = self._search_backend.search(
                SearchQuery(
                    text=query,
                    namespace=namespace,
                    status=status,
                    category=category,
                    limit=limit,
                    prompt_context=prompt_context,
                    read_only=read_only,
                )
            )
            seen_ids: set[int] = set()
            ordered_ids: list[int] = []
            for hit in hits:
                if (
                    not isinstance(hit, SearchHit)
                    or type(hit.knowledge_id) is not int
                    or hit.knowledge_id <= 0
                    or hit.knowledge_id in seen_ids
                ):
                    continue
                seen_ids.add(hit.knowledge_id)
                ordered_ids.append(hit.knowledge_id)
                if len(ordered_ids) >= 900:
                    break
            canonical_rows = self._knowledge.get_by_ids(
                ordered_ids,
                read_only=read_only,
            )
            rows_by_id = {row["id"]: row for row in canonical_rows}
            results: list[dict[str, Any]] = []
            for knowledge_id in ordered_ids:
                row = rows_by_id.get(knowledge_id)
                if row is None:
                    continue
                if namespace is not None and row.get("namespace") != namespace:
                    continue
                if status is not None and row.get("status") != status:
                    continue
                if category is not None and row.get("category") != category:
                    continue
                results.append(row)
                if len(results) >= limit:
                    break
            return results
        return self._knowledge.search(
            query,
            namespace=namespace,
            status=status,
            category=category,
            limit=limit,
            prompt_context=prompt_context,
            read_only=read_only,
        )

    @staticmethod
    def _validate_plugin_search(
        query: str,
        *,
        namespace: str | None,
        status: str | None,
        category: str | None,
        limit: int,
        prompt_context: bool,
    ) -> None:
        """Сохраняет лимиты SQLite-поиска при использовании внешнего backend."""
        maximum = 10_000 if prompt_context else 500
        if not isinstance(query, str) or not query.strip() or len(query) > maximum:
            raise ValueError(f"query должен содержать от 1 до {maximum} символов")
        if status is not None and (
            not isinstance(status, str)
            or status.strip()
            not in {
                "candidate",
                "approved",
                "rejected",
                "deprecated",
            }
        ):
            raise ValueError("Недопустимый статус знания")
        if category is not None and (
            not isinstance(category, str) or not category.strip() or len(category) > 200
        ):
            raise ValueError("category должна содержать от 1 до 200 символов")
        if namespace is not None and (
            not isinstance(namespace, str)
            or not namespace.strip()
            or len(namespace) > 200
        ):
            raise ValueError("namespace должна содержать от 1 до 200 символов")
        if not 1 <= limit <= 100:
            raise ValueError("limit должен быть от 1 до 100")

    def get(self, knowledge_id: int) -> dict[str, Any] | None:
        """Возвращает запись без изменения её жизненного цикла."""
        return self._knowledge.get(knowledge_id)

    def get_evidence(self, knowledge_id: int) -> list[dict[str, Any]]:
        """Возвращает ограниченные доказательства записи через порт памяти."""
        return self._knowledge.get_evidence(knowledge_id)

    def get_corroboration(self, knowledge_id: int) -> dict[str, Any]:
        """Возвращает данные о независимых подтверждениях записи."""
        return self._knowledge.get_corroboration(knowledge_id)

    def list_knowledge(
        self,
        *,
        status: str | None = None,
        namespace: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Возвращает список с фильтрами, заданными вызывающим адаптером."""
        return self._knowledge.list_knowledge(
            status=status,
            namespace=namespace,
            limit=limit,
        )

    def create_candidate(
        self,
        category: str,
        title: str,
        content: str,
        *,
        namespace: str,
        technology: str = "",
    ) -> dict[str, Any]:
        """Создаёт непроверенный candidate через внедрённый репозиторий."""
        return self._knowledge.create_candidate(
            category,
            title,
            content,
            namespace=namespace,
            technology=technology,
        )

    def list_candidates(
        self,
        *,
        namespace: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Перечисляет candidates в одном namespace."""
        return self._knowledge.list_candidates(namespace=namespace, limit=limit)

    def overview(self, *, namespace: str) -> dict[str, Any]:
        """Возвращает обзор одного namespace."""
        return self._knowledge.overview(namespace=namespace)

    def overview_for_namespaces(self, namespaces: Sequence[str]) -> dict[str, Any]:
        """Суммирует обзор только переданных адаптером доступных пространств."""
        visible_namespaces = tuple(sorted(set(namespaces)))
        overviews = [
            self._knowledge.overview(namespace=item) for item in visible_namespaces
        ]
        status_counts: dict[str, int] = {}
        category_counts: dict[str, int] = {}
        namespace_counts: dict[str, dict[str, Any]] = {}
        for overview in overviews:
            for status, count in overview["status_counts"].items():
                status_counts[status] = status_counts.get(status, 0) + count
            for category, count in overview["category_counts"].items():
                category_counts[category] = category_counts.get(category, 0) + count
            namespace_counts.update(overview["namespace_counts"])
        return {
            "namespace_filter": None,
            "visible_namespaces": list(visible_namespaces),
            "total": sum(overview["total"] for overview in overviews),
            "status_counts": status_counts,
            "category_counts": category_counts,
            "namespace_counts": namespace_counts,
        }


class LearningService:
    """Оркестрирует локальную проверку и безопасную обработку кандидатов."""

    def __init__(
        self,
        repository: LearningRepository,
        workflow: LearningWorkflow,
        analyzer_provider: LearningAnalysisProvider | None = None,
    ):
        """Принимает порты persistence и learning workflow."""
        self._repository = repository
        self._workflow = workflow
        self._analyzer_provider = analyzer_provider

    def verify_command(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Запускает verifier через внедрённый workflow."""
        return self._workflow.verify_command(*args, **kwargs)

    def capture_candidate(
        self,
        *,
        allowed_namespaces: Sequence[str],
        **candidate: Any,
    ) -> dict[str, Any]:
        """Сверяет namespace verifier event до candidate extraction."""
        verification_id = candidate.get("verification_id")
        if not isinstance(verification_id, str):
            raise TypeError("verification_id должен быть строкой")
        event = self._repository.get_event_by_verification(verification_id)
        if event is None:
            raise ValueError("Verification недоступна в текущем пространстве")
        namespace = self._workflow.project_namespace_from_id(event["project_id"])
        if namespace not in set(allowed_namespaces):
            raise ValueError("Verification недоступна в текущем пространстве")
        return self._workflow.submit_codex_candidate(**candidate)

    def ingest_task_event(
        self,
        event: dict[str, Any],
        *,
        allowed_namespaces: Sequence[str],
    ) -> dict[str, Any]:
        """Сохраняет только событие из разрешённого namespace без проверки статуса."""
        project_id = event.get("project_id")
        if not isinstance(project_id, str) or project_id == "global":
            raise ValueError("Learning event должен относиться к текущему проекту")
        if project_id not in set(allowed_namespaces):
            raise ValueError("Learning event недоступен из текущего пространства")
        return self._repository.ingest_task_event(event)

    def get_task_status(
        self,
        event_id: str,
        *,
        allowed_namespaces: Sequence[str],
    ) -> dict[str, Any] | None:
        """Не раскрывает статус события за пределами разрешённых пространств."""
        event = self._repository.get_task_status(event_id)
        if event is None or event.get("project_id") not in set(allowed_namespaces):
            return None
        return event

    def status_summary(self) -> dict[str, Any]:
        """Возвращает счётчики очереди из репозитория."""
        return self._repository.status_summary()

    def list_events(self, *, limit: int) -> list[dict[str, Any]]:
        """Возвращает ограниченный список событий обучения."""
        return self._repository.list_events(limit=limit)

    def list_attempts(
        self,
        *,
        limit: int,
        failed_only: bool,
    ) -> list[dict[str, Any]]:
        """Возвращает метаданные verifier без исходной команды и вывода."""
        return self._repository.list_attempts(limit=limit, failed_only=failed_only)

    def retry_event(self, event_id: str) -> None:
        """Повторно ставит допустимое событие в очередь обучения."""
        self._repository.retry_event(event_id)

    def withdraw_candidate(
        self,
        knowledge_id: int,
        *,
        reason: str,
    ) -> dict[str, Any]:
        """Отзывает candidate через audit-capable repository."""
        return self._repository.withdraw_candidate(knowledge_id, reason=reason)

    def process_pending(self, *, limit: int) -> dict[str, Any]:
        """Вызывает существующий opt-in анализатор очереди."""
        if self._analyzer_provider is None:
            return self._workflow.process_pending(limit=limit)
        return self._workflow.process_pending(
            limit=limit,
            provider=self._analyzer_provider,
        )


class SkillsService:
    """Предоставляет read/proposal use cases без human-admin операций."""

    def __init__(self, repository: SkillsRepository):
        """Принимает порт локального Skills-хранилища."""
        self._repository = repository

    def list_skills(self) -> list[dict[str, Any]]:
        return self._repository.list_skills()

    def get_skill(self, name: str) -> dict[str, Any]:
        return self._repository.get_skill(name)

    def propose_update(
        self,
        skill_name: str,
        proposer: str,
        description: str,
        content: str,
    ) -> dict[str, str]:
        """Создаёт предложение-кандидат, не меняя активный Skill."""
        proposal_id = self._repository.propose_update(
            skill_name,
            proposer,
            description,
            content,
        )
        return {"proposal_id": proposal_id, "status": "candidate"}

    def list_proposals(self, status: str = "candidate") -> list[dict[str, Any]]:
        return self._repository.list_proposals(status)

    def get_proposal(self, proposal_id: str) -> dict[str, Any]:
        return self._repository.get_proposal(proposal_id)

    def get_history(self, skill_name: str) -> list[dict[str, Any]]:
        return self._repository.get_history(skill_name)


class AdminService:
    """Инкапсулирует привилегированные переходы, не выставляя их в MCP."""

    def __init__(self, repository: AdminRepository):
        """Принимает admin-capable port, который используется только human CLI."""
        self._repository = repository

    def approve_knowledge(self, knowledge_id: int) -> dict[str, Any]:
        return self._repository.approve_knowledge(knowledge_id)

    def reject_knowledge(
        self,
        knowledge_id: int,
        *,
        reason: str,
    ) -> dict[str, Any]:
        if not reason.strip():
            raise ValueError("Причина отклонения обязательна")
        return self._repository.reject_knowledge(knowledge_id, reason=reason)

    def deprecate_knowledge(self, knowledge_id: int) -> dict[str, Any]:
        return self._repository.deprecate_knowledge(knowledge_id)

    def decide_skill_proposal(
        self,
        proposal_id: str,
        *,
        decision: str,
        maintainer: str,
        reason: str = "",
    ) -> dict[str, Any]:
        if decision == "rejected" and not reason.strip():
            raise ValueError("Причина отклонения Skill обязательна")
        return self._repository.decide_skill_proposal(
            proposal_id,
            decision=decision,
            maintainer=maintainer,
            reason=reason,
        )

    def rollback_skill_version(
        self,
        skill_name: str,
        version: str,
        *,
        maintainer: str,
    ) -> dict[str, Any]:
        return self._repository.rollback_skill_version(
            skill_name,
            version,
            maintainer=maintainer,
        )
