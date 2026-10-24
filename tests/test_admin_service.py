"""Проверяет human-admin service и явное подтверждение CLI."""

from __future__ import annotations

import builtins
from types import SimpleNamespace

from ai_engineering_memory import cli, storage
from ai_engineering_memory.application import AdminService
from ai_engineering_memory.settings import Settings
from ai_engineering_memory.sqlite_repository import LocalAdminRepository


def test_cli_knowledge_approval_requires_exact_interactive_confirmation(
    tmp_path,
    monkeypatch,
    capsys,
):
    """Несовпавшая фраза оставляет candidate без изменений."""
    database_path = tmp_path / "admin.sqlite"
    monkeypatch.setenv("AI_MEMORY_DB", str(database_path))
    store = storage.create_storage(Settings(database_path))
    with store.activate():
        candidate = store.create_knowledge(
            "backend",
            "Admin review",
            "Require an explicit human decision.",
        )

    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(builtins, "input", lambda _prompt: "APPROVE wrong-id")
    result = cli.main(["admin", "knowledge", "approve", str(candidate["id"])])

    assert result == 1
    assert store.get_knowledge(candidate["id"])["status"] == "candidate"
    assert "изменений нет" in capsys.readouterr().out


def test_cli_knowledge_approval_applies_after_exact_confirmation(
    tmp_path,
    monkeypatch,
    capsys,
):
    """Точная фраза переводит запись в approved через AdminService."""
    database_path = tmp_path / "approved.sqlite"
    monkeypatch.setenv("AI_MEMORY_DB", str(database_path))
    store = storage.create_storage(Settings(database_path))
    with store.activate():
        candidate = store.create_knowledge(
            "backend",
            "Ready candidate",
            "A maintainer reviewed this knowledge.",
        )

    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    expected = f"APPROVE {candidate['id']}"
    monkeypatch.setattr(builtins, "input", lambda _prompt: expected)
    result = cli.main(["admin", "knowledge", "approve", str(candidate["id"])])

    assert result == 0
    assert store.get_knowledge(candidate["id"])["status"] == "approved"
    assert '"status": "approved"' in capsys.readouterr().out


def test_admin_service_requires_reason_for_rejection(tmp_path):
    """Отклонение через application service невозможно без причины."""
    store = storage.create_storage(Settings(tmp_path / "reason.sqlite"))
    service = AdminService(LocalAdminRepository(store))

    try:
        service.reject_knowledge(1, reason="  ")
    except ValueError as exc:
        assert "обязательна" in str(exc)
    else:
        raise AssertionError("empty rejection reason must be rejected")


def test_cli_skill_approval_uses_temporary_skills_root(
    tmp_path,
    monkeypatch,
    capsys,
):
    """Skill approval writes only to the explicitly isolated fixture directory."""
    database_path = tmp_path / "skill-admin.sqlite"
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "sample-skill"
    skill_dir.mkdir(parents=True)
    active = (
        "---\nname: sample-skill\ndescription: Sample\nmetadata:\n"
        "  version: 1.0.0\n---\nCurrent active instructions\n"
    )
    proposed = active.replace("1.0.0", "1.1.0").replace(
        "Current active instructions",
        "Approved candidate instructions",
    )
    (skill_dir / "SKILL.md").write_text(active, encoding="utf-8")
    monkeypatch.setattr(storage, "BASE_DIR", tmp_path)
    monkeypatch.setenv("AI_MEMORY_DB", str(database_path))
    monkeypatch.setenv("AI_MEMORY_MAINTAINER", "local-owner")
    store = storage.create_storage(Settings(database_path))
    with store.activate():
        proposal_id = store.propose_skill_update(
            "sample-skill",
            "agent",
            "Clarify workflow",
            proposed,
        )

    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    expected = f"APPROVE {proposal_id}"
    monkeypatch.setattr(builtins, "input", lambda _prompt: expected)
    result = cli.main(["admin", "skill", "approve", proposal_id])

    assert result == 0
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == proposed
    with store.activate():
        assert store.list_skill_proposals("approved")[0]["id"] == proposal_id
    assert '"status": "approved"' in capsys.readouterr().out
