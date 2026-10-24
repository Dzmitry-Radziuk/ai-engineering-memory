"""Порты прикладного слоя для работы со знаниями."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol


class KnowledgeRepository(Protocol):
    """Контракт репозитория знаний, не связанный с SQLite или MCP."""

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
    ) -> list[dict[str, Any]]: ...

    def get(
        self,
        knowledge_id: int,
        *,
        read_only: bool = False,
    ) -> dict[str, Any] | None: ...

    def get_by_ids(
        self,
        knowledge_ids: Sequence[int],
        *,
        read_only: bool = False,
    ) -> list[dict[str, Any]]: ...

    def get_evidence(self, knowledge_id: int) -> list[dict[str, Any]]: ...

    def get_corroboration(self, knowledge_id: int) -> dict[str, Any]: ...

    def list_knowledge(
        self,
        *,
        status: str | None,
        namespace: str | None,
        limit: int,
    ) -> list[dict[str, Any]]: ...

    def create_candidate(
        self,
        category: str,
        title: str,
        content: str,
        *,
        namespace: str,
        technology: str,
    ) -> dict[str, Any]: ...

    def list_candidates(
        self,
        *,
        namespace: str,
        limit: int,
    ) -> list[dict[str, Any]]: ...

    def overview(self, *, namespace: str) -> dict[str, Any]: ...


class LearningRepository(Protocol):
    """Контракт доступа к очереди событий и записям локальной проверки."""

    def get_event_by_verification(
        self,
        verification_id: str,
    ) -> dict[str, Any] | None: ...

    def ingest_task_event(self, event: dict[str, Any]) -> dict[str, Any]: ...

    def get_task_status(self, event_id: str) -> dict[str, Any] | None: ...

    def status_summary(self) -> dict[str, Any]: ...

    def list_events(self, *, limit: int) -> list[dict[str, Any]]: ...

    def list_attempts(
        self,
        *,
        limit: int,
        failed_only: bool,
    ) -> list[dict[str, Any]]: ...

    def retry_event(self, event_id: str) -> None: ...

    def withdraw_candidate(
        self,
        knowledge_id: int,
        *,
        reason: str,
    ) -> dict[str, Any]: ...


class LearningWorkflow(Protocol):
    """Контракт существующих verifier и candidate-extraction сценариев."""

    def verify_command(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...

    def submit_codex_candidate(
        self,
        *,
        verification_id: str,
        title: str,
        category: str,
        summary: str,
        problem: str,
        root_cause: str,
        solution: str,
        conditions: str,
        limitations: str,
        verification_summary: str,
        evidence_refs: list[str],
        confidence: str,
        confidence_basis: str,
    ) -> dict[str, Any]: ...

    def process_pending(
        self, *, limit: int, provider: LearningAnalysisProvider | None = None
    ) -> dict[str, Any]: ...

    def project_namespace_from_id(self, project_id: str) -> str: ...


class LearningAnalysisProvider(Protocol):
    """Контракт структурированного анализа локально проверенного события."""

    def analyze(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class SkillsRepository(Protocol):
    """Контракт чтения Skills и сохранения предложений без публикации."""

    def list_skills(self) -> list[dict[str, Any]]: ...

    def get_skill(self, name: str) -> dict[str, Any]: ...

    def propose_update(
        self,
        skill_name: str,
        proposer: str,
        description: str,
        content: str,
    ) -> str: ...

    def list_proposals(self, status: str) -> list[dict[str, Any]]: ...

    def get_proposal(self, proposal_id: str) -> dict[str, Any]: ...

    def get_history(self, skill_name: str) -> list[dict[str, Any]]: ...


class AdminRepository(Protocol):
    """Контракт human-only lifecycle decisions."""

    def approve_knowledge(self, knowledge_id: int) -> dict[str, Any]: ...

    def reject_knowledge(
        self,
        knowledge_id: int,
        *,
        reason: str,
    ) -> dict[str, Any]: ...

    def deprecate_knowledge(self, knowledge_id: int) -> dict[str, Any]: ...

    def decide_skill_proposal(
        self,
        proposal_id: str,
        *,
        decision: str,
        maintainer: str,
        reason: str,
    ) -> dict[str, Any]: ...

    def rollback_skill_version(
        self,
        skill_name: str,
        version: str,
        *,
        maintainer: str,
    ) -> dict[str, Any]: ...
