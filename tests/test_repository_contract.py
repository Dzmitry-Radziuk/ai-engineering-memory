"""Контракт application service и SQLite repository для памяти."""

from __future__ import annotations

from ai_engineering_memory import storage
from ai_engineering_memory.application import MemoryService
from ai_engineering_memory.settings import Settings
from ai_engineering_memory.sqlite_repository import SqliteKnowledgeRepository
from ai_engineering_memory.storage import create_storage


def test_memory_service_preserves_candidate_approval_lifecycle(tmp_path):
    """Создание через service оставляет запись candidate и не показывает её approved."""
    store = create_storage(Settings(tmp_path / "service.sqlite"))
    store.init_database()
    service = MemoryService(SqliteKnowledgeRepository(store))

    candidate = service.create_candidate(
        "backend",
        "Bound retry policy",
        "Limit retries and retain the final error.",
        namespace="project:service",
        technology="Python",
    )

    assert candidate["status"] == "candidate"
    assert service.search(
        "bound retry", namespace="project:service", status="approved",
    ) == []
    assert service.search(
        "bound retry", namespace="project:service", status="candidate",
    )[0]["id"] == candidate["id"]
    store.add_evidence(
        candidate["id"], kind="test", result="retry boundary checked",
    )
    assert service.get_evidence(candidate["id"])[0]["result"] == (
        "retry boundary checked"
    )
    assert service.get_corroboration(candidate["id"])[
        "independent_project_count"
    ] == 0


def test_memory_service_overview_deduplicates_visible_namespaces(tmp_path):
    """Сводка не удваивает счётчики при повторе namespace во входе."""
    store = create_storage(Settings(tmp_path / "overview.sqlite"))
    store.init_database()
    service = MemoryService(SqliteKnowledgeRepository(store))
    service.create_candidate(
        "backend", "Scoped", "One record.", namespace="project:visible",
    )

    result = service.overview_for_namespaces(
        ["project:visible", "project:visible", "global"],
    )

    assert result["visible_namespaces"] == ["global", "project:visible"]
    assert result["total"] == 1


def test_memory_service_read_only_search_does_not_create_database(tmp_path):
    """Hook-поиск через application service не создаёт отсутствующую базу."""
    database_path = tmp_path / "missing.sqlite"
    service = MemoryService(
        SqliteKnowledgeRepository(create_storage(Settings(database_path))),
    )

    assert service.search(
        "retry", namespace="global", read_only=True,
    ) == []
    assert not database_path.exists()


def test_learning_service_rejects_candidate_outside_allowed_namespace():
    """Application service проверяет scope до вызова candidate workflow."""

    class Repository:
        def get_event_by_verification(self, verification_id):
            assert verification_id == "v" * 64
            return {"project_id": "opaque-project-id"}

    class Workflow:
        called = False

        def project_namespace_from_id(self, project_id):
            assert project_id == "opaque-project-id"
            return "project:other"

        def submit_codex_candidate(self, **kwargs):
            self.called = True
            return kwargs

    from ai_engineering_memory.application import LearningService

    workflow = Workflow()
    service = LearningService(Repository(), workflow)

    try:
        service.capture_candidate(
            allowed_namespaces=["project:current"],
            verification_id="v" * 64,
            title="Candidate",
        )
    except ValueError as exc:
        assert "недоступна" in str(exc)
    else:
        raise AssertionError("cross-project verification must be rejected")

    assert workflow.called is False


def test_learning_service_scopes_legacy_event_ingest_and_status():
    """MCP legacy events remain unverified and inaccessible across projects."""

    class Repository:
        saved = None

        def ingest_task_event(self, event):
            self.saved = event
            return {"event_id": event["event_id"], "status": "waiting_for_verification"}

        def get_task_status(self, event_id):
            assert event_id == "local-event"
            return {"event_id": event_id, "project_id": "project:current"}

    class Workflow:
        pass

    from ai_engineering_memory.application import LearningService

    repository = Repository()
    service = LearningService(repository, Workflow())
    event = {"event_id": "local-event", "project_id": "project:current"}

    assert service.ingest_task_event(
        event, allowed_namespaces=["global", "project:current"],
    )["status"] == "waiting_for_verification"
    assert repository.saved == event
    assert service.get_task_status(
        "local-event", allowed_namespaces=["global", "project:current"],
    )["event_id"] == "local-event"
    assert service.get_task_status(
        "local-event", allowed_namespaces=["global", "project:other"],
    ) is None
    try:
        service.ingest_task_event(
            {"event_id": "foreign-event", "project_id": "project:other"},
            allowed_namespaces=["global", "project:current"],
        )
    except ValueError as exc:
        assert "недоступен" in str(exc)
    else:
        raise AssertionError("cross-project event ingestion must be rejected")


def test_skills_service_proposal_does_not_replace_active_skill(tmp_path, monkeypatch):
    """Новая версия Skill сохраняется отдельно от активного файла."""
    from ai_engineering_memory.application import SkillsService
    from ai_engineering_memory.sqlite_repository import LocalSkillsRepository

    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "sample-skill"
    skill_dir.mkdir(parents=True)
    active = (
        "---\nname: sample-skill\ndescription: Sample\nmetadata:\n"
        "  version: 1.0.0\n---\nCurrent active instructions\n"
    )
    proposed = active.replace("1.0.0", "1.1.0").replace(
        "Current active instructions", "Proposed candidate instructions",
    )
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(active, encoding="utf-8")
    monkeypatch.setattr(storage, "_skills_root", lambda: skills_root)

    store = create_storage(Settings(tmp_path / "skills.sqlite"))
    service = SkillsService(LocalSkillsRepository(store))
    created = service.propose_update(
        "sample-skill", "agent", "Clarify workflow", proposed,
    )

    assert created["status"] == "candidate"
    review = service.get_proposal(created["proposal_id"])
    assert review["proposed_content"] == proposed
    assert review["current_content"] == active
    assert skill_path.read_text(encoding="utf-8") == active
