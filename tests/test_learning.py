"""Изолированные проверки верификации и очереди learning-событий."""

from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from ai_engineering_memory import learning, storage
from ai_engineering_memory.bootstrap import _PluginAnalyzerAdapter
from ai_engineering_memory.plugin_api import CandidateProposal


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Перенаправляет persistent-слой в одноразовую тестовую SQLite."""
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DATABASE_PATH", tmp_path / "learning.sqlite")
    monkeypatch.setenv("AI_MEMORY_DB", str(storage.DATABASE_PATH))
    monkeypatch.delenv("AI_MEMORY_OLLAMA_MODEL", raising=False)
    monkeypatch.delenv("AI_MEMORY_OLLAMA_URL", raising=False)
    storage.init_database()
    return tmp_path


def _verify(
    tmp_path,
    task_id="task-1",
    summary="Harden retry processing behavior",
    *,
    origin=True,
):
    """Создаёт Git-backed verifier event с одним новым файлом проекта."""
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=tmp_path,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )
    if origin:
        existing_origin = subprocess.run(
            ["git", "-C", str(tmp_path), "remote", "get-url", "origin"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        if existing_origin.returncode != 0:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(tmp_path),
                    "remote",
                    "add",
                    "origin",
                    f"https://example.invalid/aem-tests/{tmp_path.name}.git",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=True,
            )
    file_path = tmp_path / "src" / "module.py"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("# isolated verifier fixture\n", encoding="utf-8")
    return learning.verify_command(
        task_id,
        summary,
        [sys.executable, "-c", "print('synthetic verification output')"],
        cwd=tmp_path,
        changed_files=["src/module.py"],
    )


def test_project_namespace_is_stable_from_nested_git_directories(isolated_db, tmp_path):
    """Вложенный cwd использует namespace корня своего Git-репозитория."""
    first_root = tmp_path / "first-repo"
    nested = first_root / "src" / "feature"
    other_root = tmp_path / "other-repo"
    nested.mkdir(parents=True)
    other_root.mkdir()
    for repository in (first_root, other_root):
        subprocess.run(
            ["git", "init", "--quiet"],
            cwd=repository,
            check=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
        )

    assert learning.project_namespace(first_root) == learning.project_namespace(nested)
    assert learning.project_namespace(first_root) != learning.project_namespace(
        other_root
    )

    result = learning.verify_command(
        "nested-directory-verification",
        "Confirm nested project identity",
        [sys.executable, "-c", "pass"],
        cwd=nested,
    )
    event = storage.get_task_status(result["event_id"])
    assert event["project_id"] == learning.project_namespace(first_root)


def test_case_distinct_directories_keep_separate_project_identities(tmp_path):
    """Разный регистр путей различает проекты только если файловая система это допускает."""
    upper = tmp_path / "ProjectCase"
    lower = tmp_path / "projectcase"
    upper.mkdir()
    try:
        lower.mkdir()
    except OSError:
        pytest.skip("filesystem does not allow case-distinct sibling directories")
    if upper.resolve() == lower.resolve():
        pytest.skip("filesystem resolves case-distinct paths to the same directory")

    assert learning.project_namespace(upper) != learning.project_namespace(lower)
    assert learning.project_fingerprint(upper) is None
    assert learning.project_fingerprint(lower) is None
    assert learning._canonical_git_origin(str(upper), tmp_path) != (
        learning._canonical_git_origin(str(lower), tmp_path)
    )


def test_path_identity_preserves_case_when_sibling_collision_is_detected(
    tmp_path,
    monkeypatch,
):
    """Симулирует POSIX-каталоги, совпадающие без учёта регистра, на Windows runner."""
    upper = (tmp_path / "ProjectCase").resolve()
    lower = (tmp_path / "projectcase").resolve()
    parent = upper.parent
    expected_names = {}
    current = learning.Path(upper.anchor)
    for part in upper.parts[1:]:
        expected_names[current] = part
        current = current / part

    def iterdir_with_case_collision(directory):
        if directory == parent:
            return iter(
                (
                    SimpleNamespace(name="ProjectCase"),
                    SimpleNamespace(name="projectcase"),
                )
            )
        return iter((SimpleNamespace(name=expected_names[directory]),))

    monkeypatch.setattr(learning.Path, "iterdir", iterdir_with_case_collision)

    assert learning._path_identity_text(upper, case_sensitive=True) == str(upper)
    assert learning._path_identity_text(lower, case_sensitive=True) == str(lower)


@pytest.mark.parametrize(
    "unsafe_summary",
    [
        "Accidentally pasted AKIA1234567890ABCDEF",
        "Accidentally pasted ghp_123456789012345678901234567890123456",
        "Database is postgres://app:private-password@db.example.test/app",
        "-----BEGIN PRIVATE KEY-----",
    ],
)
def test_verifier_rejects_sensitive_summary_before_running_command(
    isolated_db,
    unsafe_summary,
):
    """Останавливает verifier до команды, если после редактирования остался секретный шаблон."""
    with pytest.raises(storage.StorageError, match="приватные данные"):
        learning.verify_command(
            "task-sensitive-summary",
            unsafe_summary,
            [sys.executable, "-c", "open('must-not-run.txt', 'w').write('ran')"],
            cwd=isolated_db,
        )

    assert not (isolated_db / "must-not-run.txt").exists()
    assert storage.learning_status_summary()["total"] == 0


def test_verifier_rejects_sensitive_changed_file_name(isolated_db):
    """Не сохраняет персональные данные, встроенные в относительное имя файла."""
    with pytest.raises(storage.StorageError, match="приватные данные"):
        learning.verify_command(
            "task-sensitive-path",
            "Validate a safely described change",
            [sys.executable, "-c", "pass"],
            cwd=isolated_db,
            changed_files=["src/owner@example.com.py"],
        )

    assert storage.learning_status_summary()["total"] == 0


def _submit_codex_lesson(verification_id, **overrides):
    """Передаёт вывод Codex со ссылкой на файл из проверенных изменений."""
    values = {
        "verification_id": verification_id,
        "title": "Recover expired worker leases without replaying completed work",
        "category": "backend",
        "summary": "Expired queue leases need bounded recovery.",
        "problem": "A worker can stop while holding a processing lease.",
        "root_cause": "The old lease remains marked processing after worker exit.",
        "solution": "Reclaim only expired leases and retain each attempt in audit.",
        "conditions": "Use for local queues with persisted processing leases.",
        "limitations": "This check covers one local SQLite project only.",
        "verification_summary": "The project test suite passed after lease recovery changes.",
        "evidence_refs": [f"verification:{verification_id}", "file:src/module.py"],
        "confidence": "medium",
        "confidence_basis": "A real successful verifier run supports this scoped result.",
    }
    values.update(overrides)
    return learning.submit_codex_candidate(**values)


class _ValidProvider:
    """Возвращает фиксированный анализ со ссылкой на переданное доказательство."""

    def analyze(self, payload):
        """Формирует безопасную фикстуру ответа анализатора."""
        verification_id = payload["verification"]["id"]
        return {
            "should_store": True,
            "title": "Use bounded retries for local workers",
            "lesson": "Bound retries and preserve a durable failure state for later recovery.",
            "category": "backend",
            "applicability": "Local queue consumers with transient provider failures.",
            "rationale": "A trusted local command exited with code zero.",
            "evidence_refs": [f"verification:{verification_id}"],
            "limitations": "Validated in one isolated project context.",
            "confidence": "medium",
            "confidence_basis": "One successful local verification supports this project-scoped candidate.",
        }


def test_verify_command_persists_only_digests_and_event(isolated_db):
    """Успешный процесс создаёт идемпотентное событие без сырых команд и вывода."""
    result = _verify(isolated_db)
    assert result["process_succeeded"] is True
    assert result["verification_scope"] == "process_exit_code_only"
    assert result["status"] == "pending_analysis"
    verification = storage.get_verification(result["verification_id"])
    event = storage.get_task_status(result["event_id"])
    assert verification["exit_code"] == 0
    assert verification["trust_status"] == "trusted_local_process"
    assert "synthetic verification output" not in json.dumps(verification)
    assert "python" not in json.dumps(verification).casefold()
    assert event["verification_id"] == result["verification_id"]
    stored_event = storage.get_learning_event_by_verification(result["verification_id"])
    stored_result = json.loads(stored_event["test_results"])
    assert stored_result["process_succeeded"] is True
    assert "passed" not in stored_result
    assert verification["project_fingerprint"].startswith("origin-v3:")
    assert len(verification["project_fingerprint"]) == len("origin-v3:") + 64
    assert str(isolated_db).casefold() not in verification["project_fingerprint"]


def test_verifier_records_only_git_changed_files(isolated_db, tmp_path):
    """Неизменённый путь не становится файловым evidence verifier."""
    project_root = tmp_path / "git-project"
    project_root.mkdir()
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=project_root,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )
    changed = project_root / "src" / "changed.py"
    ignored = project_root / "src" / "ignored.py"
    changed.parent.mkdir()
    changed.write_text("# changed for this task\n", encoding="utf-8")
    ignored.write_text("# unchanged project file\n", encoding="utf-8")
    (project_root / ".gitignore").write_text("src/ignored.py\n", encoding="utf-8")

    result = learning.verify_command(
        "changed-file-evidence",
        "Verify the current project change",
        [sys.executable, "-c", "print('verified')"],
        cwd=project_root / "src",
        changed_files=["changed.py", "ignored.py"],
    )

    assert result["recorded_changed_files"] == ["changed.py"]
    event = storage.get_learning_event_by_verification(result["verification_id"])
    assert json.loads(event["changed_files"]) == ["changed.py"]


def test_verifier_rejects_missing_and_external_file_references(isolated_db):
    """Не записывает отсутствующие файлы и пути, разрешающиеся за корень проекта."""
    project_root = isolated_db / "workspace"
    project_root.mkdir()
    command = [sys.executable, "-c", "print('verified')"]
    with pytest.raises(
        storage.StorageError, match="отсутствующий или недопустимый путь"
    ):
        learning.verify_command(
            "missing-reference",
            "Reject a missing file reference",
            command,
            cwd=project_root,
            changed_files=["src/missing.py"],
        )

    outside = isolated_db / "outside-project"
    outside.mkdir()
    outside_file = outside / "private.py"
    outside_file.write_text("# outside\n", encoding="utf-8")
    link = project_root / "external"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            pytest.skip("Ссылки на каталоги недоступны в этой среде")
        junction = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if junction.returncode != 0:
            pytest.skip("Ссылки на каталоги недоступны в этой среде")
    with pytest.raises(storage.StorageError, match="внутри проекта"):
        learning.verify_command(
            "external-reference",
            "Reject an external file reference",
            command,
            cwd=project_root,
            changed_files=["external/private.py"],
        )
    assert storage.learning_status_summary()["total"] == 0


def test_git_origin_fingerprint_discards_credentials_and_query(tmp_path):
    """Отпечаток origin не сохраняет токены и нормализует адрес репозитория."""
    identity = learning._canonical_git_origin(
        "https://user:secret@GitHub.com/team/repo.git?token=secret#private",
        tmp_path,
    )
    assert identity == "remote:github.com/team/repo"
    assert "secret" not in identity


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("https://github.com/team/repo.git", "https://github.com:443/team/repo"),
        ("http://git.example/team/repo", "http://git.example:80/team/repo.git"),
        ("ssh://git@github.com/team/repo", "ssh://git@github.com:22/team/repo.git"),
        ("git://git.example/team/repo", "git://git.example:9418/team/repo.git"),
    ],
)
def test_git_origin_fingerprint_normalizes_default_ports(tmp_path, first, second):
    """Явный стандартный порт не должен создавать ложную идентичность проекта."""
    assert learning._canonical_git_origin(first, tmp_path) == (
        learning._canonical_git_origin(second, tmp_path)
    )


def test_failed_verification_does_not_enter_analysis_queue(isolated_db):
    """Ненулевой exit code сохраняет только неуспешную попытку проверки."""
    result = learning.verify_command(
        "failed-task",
        "Try a failing local check",
        [sys.executable, "-c", "raise SystemExit(7)"],
        cwd=isolated_db,
    )
    assert result["process_succeeded"] is False
    assert result["exit_code"] == 7
    assert storage.get_task_status(result["verification_id"]) is None
    assert storage.learning_status_summary()["total"] == 0


def test_verification_timeout_is_recorded_but_not_queued(isolated_db):
    """Таймаут фиксируется как неуспешная попытка без события для анализа."""
    result = learning.verify_command(
        "timeout-task",
        "Exercise verifier timeout",
        [sys.executable, "-c", "import time; time.sleep(3)"],
        cwd=isolated_db,
        timeout=1,
    )
    assert result["process_succeeded"] is False
    assert result["error_type"] == "VerificationTimeout"
    assert result["exit_code"] == -1
    assert storage.learning_status_summary()["total"] == 0


def test_text_claim_cannot_ingest_task_verified(isolated_db):
    """MCP-подобное событие не становится verified только из-за переданного текста."""
    event = {
        "event_id": "claimed",
        "project_id": "project:0123456789abcdef",
        "task_id": "task-1",
        "client_name": "Codex",
        "task_description": "Claimed success",
        "outcome": "Done",
        "completed_at": "2026-10-22T00:00:00Z",
        "status": "task_verified",
        "test_results": {"passed": True, "evidence": "trust me"},
    }
    with pytest.raises(storage.StorageError, match="локальному verify API"):
        storage.ingest_task_event(event)


def test_mock_analysis_creates_scoped_candidate_and_deduplicates(isolated_db):
    """Mock-провайдер создаёт только проектного кандидата с локальным evidence."""
    approved = storage.create_knowledge(
        "ops",
        "Approved invariant",
        "Existing maintainer-approved record",
        namespace="global",
    )
    storage.approve_knowledge(approved["id"])
    first = _verify(isolated_db, "task-1")
    outcome = learning.process_pending(provider=_ValidProvider())
    assert outcome["outcomes"][0]["status"] == "candidate_created"
    candidate_id = outcome["outcomes"][0]["knowledge_id"]
    candidate = storage.get_knowledge(candidate_id)
    assert candidate["status"] == "candidate"
    assert candidate["source_type"] == "learning_engine"
    assert candidate["namespace"].startswith("project:")
    preserved = storage.get_knowledge(approved["id"])
    assert preserved["status"] == "approved"
    assert preserved["content"] == "Existing maintainer-approved record"
    with storage._connect() as connection:
        evidence = connection.execute(
            "SELECT * FROM evidence WHERE knowledge_id=?", (candidate_id,)
        ).fetchone()
    assert "verification:" in evidence["source"]
    assert storage.get_task_status(first["event_id"])["status"] == "candidate_created"

    second = _verify(isolated_db, "task-2")
    duplicate = learning.process_pending(provider=_ValidProvider())
    assert duplicate["outcomes"][0]["status"] == "duplicate"
    assert storage.get_task_status(second["event_id"])["status"] == "no_insight"
    assert len(storage.list_knowledge(status="candidate")) == 1


def test_analyzer_plugin_flows_through_verified_candidate_validation(isolated_db):
    """Analyzer SPI получает обезличенный запрос, а ядро создаёт только candidate."""

    class Analyzer:
        plugin_api_version = 1

        def __init__(self):
            self.requests = []

        def analyze(self, request):
            self.requests.append(request)
            return CandidateProposal(
                title="Keep bounded retries for transient failures",
                category="backend",
                content="Bound retries and retain durable failure state for later recovery.",
                rationale="A trusted local verifier completed successfully.",
            )

    verification = _verify(isolated_db)
    assert verification["recorded_changed_files"] == ["src/module.py"]
    analyzer = Analyzer()
    result = learning.process_pending(provider=_PluginAnalyzerAdapter(analyzer))

    assert analyzer.requests[0].project_id.startswith("project:")
    assert analyzer.requests[0].summary == "Harden retry processing behavior"
    assert analyzer.requests[0].changed_files == ("src/module.py",)
    assert result["outcomes"][0]["status"] == "candidate_created", result
    candidate = storage.get_knowledge(result["outcomes"][0]["knowledge_id"])
    assert candidate["status"] == "candidate"
    verified_event = storage.get_task_status(verification["event_id"])
    assert candidate["namespace"] == learning.project_namespace_from_id(
        verified_event["project_id"]
    )
    with storage._connect() as connection:
        evidence = connection.execute(
            "SELECT source FROM evidence WHERE knowledge_id=?", (candidate["id"],)
        ).fetchone()
    assert verification["verification_id"] in evidence["source"]


def test_unconfigured_provider_leaves_verified_event_pending(isolated_db):
    """Без явной локальной модели обработчик не вызывает сеть и не меняет очередь."""
    result = _verify(isolated_db)
    outcome = learning.process_pending()
    assert outcome == {"processed": 0, "provider": "not configured", "outcomes": []}
    assert storage.get_task_status(result["event_id"])["status"] == "pending_analysis"


def test_codex_candidate_requires_verifier_and_is_project_scoped(isolated_db):
    """Codex submits one validated candidate tied to the successful local verifier."""
    result = _verify(isolated_db)
    candidate = _submit_codex_lesson(result["verification_id"])
    assert candidate["status"] == "candidate"
    assert candidate["namespace"] == learning.project_namespace(isolated_db)
    assert candidate["idempotent"] is False
    saved = storage.get_knowledge(candidate["knowledge_id"])
    assert saved["status"] == "candidate"
    assert saved["source_type"] == "learning_engine"
    assert saved["source_reference"] == f"event:{result['event_id']}"
    assert "Проблема:" in saved["content"] and "Решение:" in saved["content"]
    assert "exit code 0" in saved["content"]
    assert (
        "Комментарий Codex о проверке (не подтверждён verifier-ом)" in saved["content"]
    )
    evidence = storage.get_knowledge_evidence(candidate["knowledge_id"])[0]
    assert "Факт verifier-а" in evidence["result"]
    assert "полнота проверки не оценивались" in evidence["result"]
    assert "Комментарий модели (не подтверждён verifier-ом)" in evidence["result"]
    assert storage.get_task_status(result["event_id"])["status"] == "candidate_created"
    repeated = _submit_codex_lesson(result["verification_id"])
    assert repeated["knowledge_id"] == candidate["knowledge_id"]
    assert repeated["idempotent"] is True
    assert (
        len(
            storage.list_knowledge(status="candidate", namespace=candidate["namespace"])
        )
        == 1
    )


def test_failed_and_timeout_verifications_cannot_create_candidate(isolated_db):
    """У неуспешной попытки нет verified task event для MCP capture."""
    failed = learning.verify_command(
        "failed-candidate",
        "Attempt a failing verifier",
        [sys.executable, "-c", "raise SystemExit(9)"],
        cwd=isolated_db,
    )
    timed_out = learning.verify_command(
        "timeout-candidate",
        "Attempt a timed out verifier",
        [sys.executable, "-c", "import time; time.sleep(3)"],
        cwd=isolated_db,
        timeout=1,
    )
    for attempt in (failed, timed_out):
        assert attempt["process_succeeded"] is False
        assert attempt["verification_scope"] == "process_exit_code_only"
        with pytest.raises(
            storage.StorageError, match="нет события локального verifier"
        ):
            _submit_codex_lesson(attempt["verification_id"])
    assert storage.list_knowledge(status="candidate") == []


def test_candidate_rejects_secret_wrong_evidence_and_trivial_lesson(isolated_db):
    """Проверяются приватные поля, чужой file-ref и слишком общий вывод."""
    result = _verify(isolated_db)
    with pytest.raises(storage.StorageError, match="приватные данные"):
        _submit_codex_lesson(
            result["verification_id"], solution="Set token=FAKE_SECRET_123"
        )
    with pytest.raises(storage.StorageError, match="проверенных файлов"):
        _submit_codex_lesson(
            result["verification_id"],
            evidence_refs=[
                f"verification:{result['verification_id']}",
                "file:src/not-changed.py",
            ],
        )
    with pytest.raises(storage.StorageError, match="слишком общий"):
        _submit_codex_lesson(
            result["verification_id"],
            summary="Fix it.",
            problem="Small bug.",
            solution="Change code.",
        )
    assert storage.list_knowledge(status="candidate") == []


@pytest.mark.parametrize(
    "sensitive_value",
    [
        "AKIA1234567890ABCDEF",
        "ghp_123456789012345678901234567890123456",
        "postgres://app:private-password@db.example.test/app",
        "https://user:private-password@example.test/docs",
        r"C:\Users\Owner\private.txt",
    ],
)
def test_learning_validators_share_sensitive_data_policy(sensitive_value):
    """Codex capture и анализатор одинаково отклоняют типовые приватные данные."""
    with pytest.raises(storage.StorageError, match="приватные данные"):
        learning._validate_codex_candidate_text("lesson", sensitive_value, 1200)

    analysis = {
        "should_store": True,
        "title": "Use bounded retries for transient failures",
        "lesson": f"{sensitive_value} Bound retries and preserve failure state.",
        "category": "backend",
        "applicability": "Local queue consumers with transient failures.",
        "rationale": "A trusted local command exited successfully.",
        "evidence_refs": ["verification:verified-id"],
        "limitations": "Validated in one isolated project context.",
        "confidence": "medium",
        "confidence_basis": "One successful local verification supports this scoped result.",
    }
    with pytest.raises(learning.ProviderError, match="SensitiveAnalysisRejected"):
        learning._validate_analysis(analysis, "verified-id")


def test_cross_project_rollup_requires_file_evidence(isolated_db):
    """Необоснованная ссылка только на verifier не поддерживает global-кандидат."""
    first = _verify(isolated_db, "candidate-without-file-reference")
    first_candidate = _submit_codex_lesson(
        first["verification_id"],
        evidence_refs=[f"verification:{first['verification_id']}"],
    )
    assert first_candidate["status"] == "candidate"
    assert first_candidate["global_candidate_id"] is None

    other_project = isolated_db / "file-supported-project"
    other_project.mkdir()
    other = _verify(other_project, "candidate-with-file-reference")
    other_candidate = _submit_codex_lesson(other["verification_id"])

    assert other_candidate["status"] == "candidate"
    assert other_candidate["global_candidate_id"] is None
    assert storage.memory_overview(namespace="global")["total"] == 0


def test_candidate_rolls_up_after_two_independent_projects(isolated_db):
    """Повторяемый урок создаёт global candidate после двух project-контекстов."""
    first = _verify(isolated_db, "candidate-first")
    candidate = _submit_codex_lesson(first["verification_id"])
    same_project = _verify(isolated_db, "candidate-repeat")
    duplicate = _submit_codex_lesson(same_project["verification_id"])
    assert duplicate["status"] == "duplicate_skipped"
    assert duplicate["knowledge_id"] == candidate["knowledge_id"]
    assert storage.get_task_status(same_project["event_id"])["status"] == "no_insight"

    other_project = isolated_db / "other-project"
    other_project.mkdir()
    other = _verify(other_project, "candidate-other-project")
    other_candidate = _submit_codex_lesson(other["verification_id"])
    assert other_candidate["status"] == "candidate"
    assert other_candidate["namespace"] == learning.project_namespace(other_project)
    assert other_candidate["namespace"] != candidate["namespace"]
    global_id = other_candidate["global_candidate_id"]
    assert global_id is not None
    global_record = storage.get_knowledge(global_id)
    assert global_record["namespace"] == "global"
    assert global_record["status"] == "candidate"
    corroboration = storage.get_learning_corroboration(global_id)
    assert corroboration["independent_project_count"] == 2
    assert {item["project_namespace"] for item in corroboration["supports"]} == {
        candidate["namespace"],
        other_candidate["namespace"],
    }

    third_project = isolated_db / "third-project"
    third_project.mkdir()
    third = _verify(third_project, "candidate-third-project")
    third_result = _submit_codex_lesson(third["verification_id"])
    assert third_result["status"] == "duplicate_skipped"
    assert third_result["global_candidate_id"] == global_id
    assert third_result["independent_project_count"] == 3
    assert storage.get_knowledge(global_id)["status"] == "candidate"

    with storage._connect() as connection:
        namespaces = connection.execute(
            "SELECT project_namespace FROM learning_corroborations "
            "WHERE global_knowledge_id=? ORDER BY project_namespace",
            (global_id,),
        ).fetchall()
        for index, row in enumerate(namespaces):
            connection.execute(
                "UPDATE learning_corroborations SET project_fingerprint=? "
                "WHERE global_knowledge_id=? AND project_namespace=?",
                (f"origin-v2:{index:064x}", global_id, row["project_namespace"]),
            )
    legacy_support = storage.get_learning_corroboration(global_id)
    assert legacy_support["independent_project_count"] == 0
    assert legacy_support["legacy_identity_support_count"] == 3

    fourth_project = isolated_db / "current-origin-after-legacy"
    fourth_project.mkdir()
    fourth = _verify(fourth_project, "candidate-fourth-project")
    legacy_duplicate = _submit_codex_lesson(fourth["verification_id"])
    assert legacy_duplicate["status"] == "duplicate_skipped"
    assert legacy_duplicate["global_candidate_id"] == global_id
    assert legacy_duplicate["independent_project_count"] == 1
    updated_support = storage.get_learning_corroboration(global_id)
    assert updated_support["independent_project_count"] == 1
    assert updated_support["legacy_identity_support_count"] == 3


def test_projects_without_git_origin_do_not_roll_up_globally(isolated_db):
    """Разные локальные пути без origin не доказывают независимость проектов."""
    first_root = isolated_db / "local-copy-a"
    first_root.mkdir()
    first = _verify(first_root, "local-copy-first", origin=False)
    first_candidate = _submit_codex_lesson(first["verification_id"])
    assert first_candidate["status"] == "candidate"
    assert (
        storage.get_verification(first["verification_id"])["project_fingerprint"]
        is None
    )

    second_root = isolated_db / "local-copy-b"
    second_root.mkdir()
    second = _verify(second_root, "local-copy-second", origin=False)
    second_candidate = _submit_codex_lesson(second["verification_id"])

    assert second_candidate["status"] == "candidate"
    assert second_candidate["global_candidate_id"] is None
    assert second_candidate["independent_project_count"] == 0
    assert storage.memory_overview(namespace="global")["total"] == 0


def test_legacy_path_fingerprint_cannot_support_new_global_candidate(isolated_db):
    """Отпечаток старого формата не считается независимым origin evidence."""
    first_root = isolated_db / "legacy-project"
    first_root.mkdir()
    first = _verify(first_root, "legacy-origin-first")
    _submit_codex_lesson(first["verification_id"])
    with storage._connect() as connection:
        connection.execute(
            "UPDATE verification_records SET project_fingerprint=? WHERE verification_id=?",
            ("a" * 64, first["verification_id"]),
        )

    second_root = isolated_db / "current-project"
    second_root.mkdir()
    second = _verify(second_root, "current-origin-second")
    candidate = _submit_codex_lesson(second["verification_id"])

    assert candidate["status"] == "candidate"
    assert candidate["global_candidate_id"] is None
    assert storage.memory_overview(namespace="global")["total"] == 0


def test_clones_with_same_git_origin_do_not_corroborate_each_other(
    isolated_db,
    monkeypatch,
):
    """Разные каталоги одного origin считаются одним проектом для общей памяти."""
    origins = {
        "clone-a": "remote:example.org/team/shared-repo",
        "clone-b": "remote:example.org/team/shared-repo",
        "clone-c": "remote:example.org/team/shared-repo",
        "other-project": "remote:example.org/team/other-repo",
    }
    monkeypatch.setattr(
        learning,
        "_git_origin_identity",
        lambda root: origins[root.name],
    )
    clone_a = isolated_db / "clone-a"
    clone_b = isolated_db / "clone-b"
    clone_c = isolated_db / "clone-c"
    other_project = isolated_db / "other-project"
    for project in (clone_a, clone_b, clone_c, other_project):
        project.mkdir()

    first = _verify(clone_a, "same-origin-first")
    first_candidate = _submit_codex_lesson(first["verification_id"])
    same_origin = _verify(clone_b, "same-origin-clone")
    duplicate = _submit_codex_lesson(same_origin["verification_id"])

    assert duplicate["status"] == "duplicate_skipped"
    assert duplicate["global_candidate_id"] is None
    assert storage.memory_overview(namespace="global")["total"] == 0

    independent = _verify(other_project, "different-origin")
    corroborated = _submit_codex_lesson(independent["verification_id"])
    assert corroborated["global_candidate_id"] is not None
    assert corroborated["independent_project_count"] == 2
    assert first_candidate["namespace"] != corroborated["namespace"]

    repeated_clone = _verify(clone_c, "same-origin-after-rollup")
    repeated = _submit_codex_lesson(repeated_clone["verification_id"])
    assert repeated["status"] == "duplicate_skipped"
    assert repeated["independent_project_count"] == 2


def test_candidate_dedupe_skips_approved_global_lesson(isolated_db):
    """Approved global knowledge also prevents an equivalent project candidate."""
    first = _verify(isolated_db, "global-seed")
    candidate = _submit_codex_lesson(first["verification_id"])
    scoped = storage.get_knowledge(candidate["knowledge_id"])
    global_record = storage.create_knowledge(
        scoped["category"],
        scoped["title"],
        scoped["content"],
        namespace="global",
    )
    storage.approve_knowledge(global_record["id"])

    second_project = isolated_db / "global-match-project"
    second_project.mkdir()
    second = _verify(second_project, "global-match")
    duplicate = _submit_codex_lesson(second["verification_id"])
    assert duplicate["status"] == "duplicate_skipped"
    assert duplicate["knowledge_id"] == global_record["id"]
    assert duplicate["namespace"] == learning.project_namespace(second_project)
    assert storage.get_knowledge(global_record["id"])["status"] == "approved"
    assert (
        storage.get_learning_corroboration(global_record["id"])[
            "independent_project_count"
        ]
        == 1
    )


def test_stale_processing_event_is_recovered_once(isolated_db):
    """Просроченный lock восстанавливается, а актуальный захват исключает дубль."""
    result = _verify(isolated_db)
    with storage._connect() as connection:
        connection.execute(
            "UPDATE task_events SET status='processing',attempt_count=1,"
            "locked_at='2000-01-01T00:00:00+00:00' "
            "WHERE event_id=?",
            (result["event_id"],),
        )
    recovered = storage.claim_learning_event(stale_after_seconds=60)
    assert recovered["event_id"] == result["event_id"]
    assert recovered["attempt_count"] == 2
    assert storage.claim_learning_event(stale_after_seconds=60) is None


def test_exhausted_stale_event_becomes_permanent_error(isolated_db):
    """Зависшая запись после лимита попыток закрывается с диагностикой."""
    result = _verify(isolated_db)
    with storage._connect() as connection:
        connection.execute(
            "UPDATE task_events SET status='processing',attempt_count=3,"
            "locked_at='2000-01-01T00:00:00+00:00' WHERE event_id=?",
            (result["event_id"],),
        )
    assert storage.claim_learning_event(stale_after_seconds=60) is None
    event = storage.get_task_status(result["event_id"])
    assert event["status"] == "permanent_error"
    assert event["last_error_type"] == "ProcessingLeaseExpired"


def test_cli_verify_accepts_exact_argv_and_enqueues(isolated_db, capsys):
    """CLI-адаптер принимает только указанную argv-команду и печатает метаданные."""
    from ai_engineering_memory import cli

    assert (
        cli.main(
            [
                "learning",
                "verify",
                "--task-id",
                "cli-task",
                "--summary",
                "Exercise the local verification CLI",
                "--cwd",
                str(isolated_db),
                "--",
                sys.executable,
                "-c",
                "print('ok')",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["process_succeeded"] is True
    assert output["verification_scope"] == "process_exit_code_only"
    assert storage.get_task_status(output["event_id"])["status"] == "pending_analysis"


def test_invalid_analysis_is_retryable_and_localhost_only(isolated_db):
    """Невалидный JSON-ответ идёт на ограниченный повтор, удалённый URL запрещён."""
    result = _verify(isolated_db)

    class InvalidProvider:
        """Имитирует ответ, не прошедший JSON-контракт."""

        def analyze(self, payload):
            """Возвращает некорректную структуру."""
            return {"should_store": True, "title": "Only a title"}

    outcome = learning.process_pending(provider=InvalidProvider())
    status = storage.get_task_status(result["event_id"])
    assert outcome["outcomes"][0]["status"] == "retryable_error"
    assert status["attempt_count"] == 1
    assert status["last_error_type"] == "ProviderError"
    storage.retry_learning_event(result["event_id"])
    retried = storage.get_task_status(result["event_id"])
    assert retried["attempt_count"] == 0
    assert retried["status"] == "pending_analysis"
    with pytest.raises(learning.ProviderError, match="InvalidLocalProviderConfig"):
        learning.OllamaProvider("model", "https://example.com/api/generate")


def test_candidate_withdrawal_keeps_audit_trail(isolated_db):
    """Отмена собственного кандидата сохраняет запись и причину в audit trail."""
    _verify(isolated_db)
    result = learning.process_pending(provider=_ValidProvider())
    candidate_id = result["outcomes"][0]["knowledge_id"]
    withdrawn = storage.withdraw_learning_candidate(
        candidate_id, reason="Duplicate finding"
    )
    assert withdrawn["status"] == "rejected"
    with storage._connect() as connection:
        audit = connection.execute(
            "SELECT action,details FROM learning_event_audit WHERE knowledge_id=? "
            "ORDER BY id DESC LIMIT 1",
            (candidate_id,),
        ).fetchone()
    assert audit["action"] == "candidate_withdrawn"
    assert "Duplicate finding" in audit["details"]
