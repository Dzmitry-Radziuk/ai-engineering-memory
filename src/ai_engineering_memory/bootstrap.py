""Composition root для CLI/MCP адаптеров локального приложения."""

from __future__ import annotations

from ai_engineering_memory import storage
from ai_engineering_memory.application import (
    AdminService,
    LearningService,
    MemoryService,
    SkillsService,
)
from ai_engineering_memory.sqlite_repository import (
    LocalAdminRepository,
    LocalSkillsRepository,
    SqliteKnowledgeRepository,
    SqliteLearningRepository,
)


def create_memory_service() -> MemoryService:
    """Создаёт memory service на хранилище текущего execution context."""
    return MemoryService(SqliteKnowledgeRepository(storage.create_storage()))


def create_learning_service() -> LearningService:
    """Создаёт learning service с SQLite repository и текущим workflow."""
    from ai_engineering_memory import learning

    return LearningService(
        SqliteLearningRepository(storage.create_storage()),
        learning,
    )


def create_skills_service() -> SkillsService:
    """Создаёт Skills service на storage facade текущего запуска."""
    return SkillsService(LocalSkillsRepository(storage.create_storage()))


def create_admin_service() -> AdminService:
    """Создаёт privileged service для вызова только из human CLI."""
    return AdminService(LocalAdminRepository(storage.create_storage()))
