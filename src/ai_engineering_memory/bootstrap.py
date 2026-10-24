"""Composition root для CLI/MCP адаптеров локального приложения."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from ai_engineering_memory import storage
from ai_engineering_memory.application import (
    AdminService,
    LearningService,
    MemoryService,
    SkillsService,
)
from ai_engineering_memory.plugin_api import (
    AnalysisRequest,
    AnalyzerProvider,
    CandidateProposal,
    SearchBackend,
)
from ai_engineering_memory.plugins import PluginRegistry
from ai_engineering_memory.privacy import contains_sensitive_text
from ai_engineering_memory.settings import Settings
from ai_engineering_memory.sqlite_repository import (
    LocalAdminRepository,
    LocalSkillsRepository,
    SqliteKnowledgeRepository,
    SqliteLearningRepository,
)


class _PluginAnalyzerAdapter:
    """Адаптирует plugin SPI к общей проверке learning-кандидатов."""

    def __init__(self, provider: AnalyzerProvider) -> None:
        """Сохраняет явно выбранный analyzer provider."""
        self._provider = provider

    def analyze(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Передаёт analyzer краткий текст и проверенные пути изменений."""
        verification = payload.get("verification")
        project_id = payload.get("project_namespace")
        summary = payload.get("task_summary")
        if (
            not isinstance(verification, dict)
            or not isinstance(verification.get("id"), str)
            or not isinstance(project_id, str)
            or not isinstance(summary, str)
        ):
            raise TypeError("InvalidVerifiedLearningPayload")
        raw_files = payload.get("changed_files")
        changed_files: list[str] = []
        if isinstance(raw_files, list):
            for path in raw_files[:50]:
                if (
                    isinstance(path, str)
                    and 0 < len(path) <= 300
                    and "\\" not in path
                    and ":" not in path
                    and not PurePosixPath(path).is_absolute()
                    and ".." not in PurePosixPath(path).parts
                    and not contains_sensitive_text(path.replace("/", "\\"))
                ):
                    changed_files.append(path)

        proposal = self._provider.analyze(
            AnalysisRequest(
                project_id=project_id,
                summary=summary[:2_000],
                changed_files=tuple(changed_files),
            )
        )
        should_store = proposal is not None
        if proposal is not None and not isinstance(proposal, CandidateProposal):
            raise TypeError("InvalidAnalyzerProposal")
        return {
            "should_store": should_store,
            "title": proposal.title if proposal else "No reusable lesson found",
            "lesson": proposal.content
            if proposal
            else "No reusable lesson was identified.",
            "category": proposal.category if proposal else "backend",
            "applicability": (
                "A single verified task in this project context."
                if proposal
                else "No applicable lesson identified."
            ),
            "rationale": proposal.rationale
            if proposal
            else "The analyzer found no reusable lesson.",
            "evidence_refs": [f"verification:{verification['id']}"],
            "limitations": (
                "Evidence is limited to one successful local verification."
                if proposal
                else "No lesson was proposed."
            ),
            "confidence": "low",
            "confidence_basis": (
                "One trusted local verification supports a project-scoped candidate."
            ),
        }


def create_memory_service() -> MemoryService:
    """Создаёт memory service на хранилище текущего execution context."""
    settings = Settings.from_environment(repository_data_dir=storage.DATA_DIR)
    search_backend: SearchBackend | None = None
    if settings.search_plugin:
        search_backend = PluginRegistry().load(
            "ai_engineering_memory.search",
            settings.search_plugin,
        )
    return MemoryService(
        SqliteKnowledgeRepository(storage.create_storage()),
        search_backend=search_backend,
    )


def create_learning_service() -> LearningService:
    """Создаёт learning service с SQLite repository и текущим workflow."""
    from ai_engineering_memory import learning

    settings = Settings.from_environment(repository_data_dir=storage.DATA_DIR)
    analyzer_provider: _PluginAnalyzerAdapter | None = None
    if settings.analyzer_plugin:
        provider = PluginRegistry().load(
            "ai_engineering_memory.analyzers",
            settings.analyzer_plugin,
        )
        analyzer_provider = _PluginAnalyzerAdapter(provider)
    return LearningService(
        SqliteLearningRepository(storage.create_storage()),
        learning,  # type: ignore[arg-type]  # storage facade satisfies workflow protocol.
        analyzer_provider=analyzer_provider,
    )


def create_skills_service() -> SkillsService:
    """Создаёт Skills service на storage facade текущего запуска."""
    return SkillsService(LocalSkillsRepository(storage.create_storage()))


def create_admin_service() -> AdminService:
    """Создаёт privileged service для вызова только из human CLI."""
    return AdminService(LocalAdminRepository(storage.create_storage()))
