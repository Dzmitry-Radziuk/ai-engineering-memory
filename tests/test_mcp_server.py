"""Проверка реального запуска MCP stdio-клиента и локальных инструментов."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from ai_engineering_memory.learning import project_namespace


def test_stdio_server_exposes_tools_and_uses_isolated_database(tmp_path):
    """Запускает дочерний MCP-процесс на одноразовой базе и вызывает инструменты."""
    database = tmp_path / "mcp.sqlite"
    project_root = tmp_path / "verified-project"
    project_root.mkdir()
    foreign_root = tmp_path / "foreign-project"
    foreign_root.mkdir()
    for repository in (project_root, foreign_root):
        subprocess.run(
            ["git", "init", "--quiet"], cwd=repository, check=True,
            stdin=subprocess.DEVNULL, capture_output=True,
        )
    active_namespace = project_namespace(project_root)
    foreign_namespace = project_namespace(foreign_root)
    environment = os.environ.copy()
    environment["AI_MEMORY_DB"] = str(database)
    environment["AI_MEMORY_PROJECT_ROOT"] = str(project_root)
    environment["AI_MEMORY_MAINTAINER"] = "mcp-test-owner"
    proposal_content = (
        "---\nname: mcp-review-skill\ndescription: MCP review fixture\n"
        "---\nProposed untrusted instructions\n"
    )
    proposal_seed = subprocess.run(
        [
            sys.executable, "-c",
            (
                "import sys; from ai_engineering_memory import storage; "
                "print(storage.propose_skill_update('mcp-review-skill','test',"
                "'Verify read-only proposal inspection',sys.argv[1]))"
            ),
            proposal_content,
        ],
        env=environment, capture_output=True, text=True, check=True,
    )
    skill_proposal_id = proposal_seed.stdout.strip()
    verification = subprocess.run(
        [
            sys.executable, "-m", "ai_engineering_memory.cli", "learning", "verify",
            "--task-id", "mcp-verified-candidate", "--summary",
            "Exercise isolated MCP candidate capture", "--cwd", str(project_root), "--",
            sys.executable, "-c", "assert 2 + 2 == 4",
        ],
        env=environment, capture_output=True, text=True, check=True,
    )
    verifier_result = json.loads(verification.stdout)
    foreign_verification = subprocess.run(
        [
            sys.executable, "-m", "ai_engineering_memory.cli", "learning", "verify",
            "--task-id", "mcp-foreign-candidate", "--summary",
            "Exercise foreign MCP candidate rejection", "--cwd", str(foreign_root), "--",
            sys.executable, "-c", "assert 3 + 3 == 6",
        ],
        env=environment, capture_output=True, text=True, check=True,
    )
    foreign_verifier_result = json.loads(foreign_verification.stdout)
    foreign_seed = subprocess.run(
        [sys.executable, "-c", (
            "from ai_engineering_memory import storage; "
            "item=storage.create_knowledge('test','Foreign confidential',"
            f"'Do not expose this project',namespace='{foreign_namespace}'); "
            "print(item['id'])"
        )],
        env=environment, capture_output=True, text=True, check=True,
    )
    foreign_knowledge_id = int(foreign_seed.stdout.strip())

    async def _exercise() -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "ai_engineering_memory.cli", "mcp", "serve"],
            env=environment,
        )
        async with stdio_client(params) as (read_stream, write_stream), ClientSession(
            read_stream, write_stream
        ) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert {
                "memory_search", "memory_overview", "memory_add_candidate", "learning_submit_task",
                "learning_get_status", "learning_add_candidate",
                "learning_get_corroboration", "skills_get_proposal",
            } <= names
            assert "learning_capture_verified" not in names
            proposal_review = await session.call_tool("skills_get_proposal", {
                "proposal_id": skill_proposal_id,
            })
            assert not proposal_review.isError
            assert proposal_review.structuredContent["proposed_content"] == proposal_content
            assert proposal_review.structuredContent["current_content"] is None
            created = await session.call_tool("memory_add_candidate", {
                "category": "test", "title": "MCP smoke", "content": "isolated tool call",
                "namespace": "global",
            })
            assert not created.isError
            smoke_id = created.structuredContent["id"]
            default_search = await session.call_tool("memory_search", {"query": "MCP smoke"})
            assert not default_search.isError
            assert "MCP smoke" not in str(default_search.content)
            foreign_search = await session.call_tool("memory_search", {
                "query": "MCP smoke", "namespace": foreign_namespace,
                "status": None,
            })
            assert foreign_search.isError
            foreign_write = await session.call_tool("memory_add_candidate", {
                "category": "test", "title": "Foreign scope write",
                "content": "must be denied", "namespace": foreign_namespace,
            })
            assert foreign_write.isError
            foreign_list = await session.call_tool("memory_list_candidates", {
                "namespace": foreign_namespace,
            })
            assert foreign_list.isError
            foreign_overview = await session.call_tool("memory_overview", {
                "namespace": foreign_namespace,
            })
            assert foreign_overview.isError
            foreign_event = await session.call_tool("learning_submit_task", {
                "event_json": json.dumps({
                    "event_id": "foreign-project-event",
                    "project_id": foreign_namespace,
                    "task_id": "foreign-task",
                    "client_name": "Codex",
                    "task_description": "Must not cross the active MCP scope",
                    "outcome": "Tested",
                    "completed_at": "2026-10-22T00:00:00Z",
                    "status": "task_completed",
                    "test_results": {},
                }),
            })
            assert foreign_event.isError
            prompt_search = await session.call_tool("memory_search", {
                "query": "Can you find the MCP smoke configuration? " + "details " * 100,
                "status": "approved", "namespace": "global", "limit": 5,
                "prompt_context": True,
            })
            assert not prompt_search.isError
            assert "MCP smoke" not in str(prompt_search.content)
            approved_candidate = await session.call_tool("memory_add_candidate", {
                "category": "backend", "title": "Retry Support",
                "content": "MCP hook uses retry support for transient API failures",
                "namespace": "global",
            })
            assert not approved_candidate.isError
            retry_support_id = approved_candidate.structuredContent["id"]
            unauthorized_approval = await session.call_tool("memory_approve", {
                "knowledge_id": retry_support_id,
                "maintainer": "local-owner",
                "confirmation": f"APPROVE {retry_support_id}",
            })
            assert unauthorized_approval.isError
            approved = await session.call_tool("memory_approve", {
                "knowledge_id": retry_support_id,
                "maintainer": "mcp-test-owner",
                "confirmation": f"APPROVE {retry_support_id}",
            })
            assert not approved.isError
            hidden = await session.call_tool("memory_get", {
                "knowledge_id": foreign_knowledge_id,
            })
            assert not hidden.isError
            assert "Foreign confidential" not in str(hidden.content)
            forbidden_approval = await session.call_tool("memory_approve", {
                "knowledge_id": foreign_knowledge_id,
                "maintainer": "local-owner",
                "confirmation": f"APPROVE {foreign_knowledge_id}",
            })
            assert forbidden_approval.isError
            scoped_overview = await session.call_tool("memory_overview", {})
            assert not scoped_overview.isError
            assert foreign_namespace not in str(scoped_overview.content)
            prompt_search = await session.call_tool("memory_search", {
                "query": "Please find Retry Support configuration " + "details " * 100,
                "status": "approved", "namespace": "global", "limit": 5,
                "prompt_context": True,
            })
            assert not prompt_search.isError
            assert "Retry Support" in str(prompt_search.content)
            candidate_search = await session.call_tool("memory_search", {
                "query": "MCP smoke", "status": "candidate", "category": "test",
                "namespace": "global",
            })
            assert not candidate_search.isError
            assert "MCP smoke" in str(candidate_search.content)
            all_status_search = await session.call_tool("memory_search", {
                "query": "MCP smoke", "status": None, "category": "test",
                "namespace": "global",
            })
            assert not all_status_search.isError
            assert "MCP smoke" in str(all_status_search.content)
            listed = await session.call_tool("memory_list_candidates", {})
            assert not listed.isError
            assert "MCP smoke" in str(listed.content)
            overview = await session.call_tool("memory_overview", {})
            assert not overview.isError
            assert "candidate" in str(overview.content)
            assert "test" in str(overview.content)
            assert "global" in str(overview.content)

            lesson = {
                "verification_id": verifier_result["verification_id"],
                "title": "Keep isolated MCP writes tied to local verifier events",
                "category": "agent_workflow",
                "summary": "Candidate capture needs a local verification reference.",
                "problem": "A model can submit a completion claim without a trusted process.",
                "root_cause": "Text claims do not identify a recorded verifier result.",
                "solution": "Require a successful verification ID stored by the local CLI.",
                "conditions": "Use for local MCP candidate submissions from Codex.",
                "limitations": "A zero exit code does not prove the test was sufficient.",
                "verification_summary": "The isolated Python assertion exited successfully.",
                "evidence_refs": [f"verification:{verifier_result['verification_id']}"],
                "confidence": "medium",
                "confidence_basis": "The test exercised the real local stdio MCP server.",
            }
            added = await session.call_tool("learning_add_candidate", lesson)
            assert not added.isError
            added_result = added.structuredContent
            assert added_result["status"] == "candidate"
            assert added_result["namespace"] == active_namespace
            assert added_result["verification_id"] == verifier_result["verification_id"]
            assert added_result["global_candidate_id"] is None
            repeated = await session.call_tool("learning_add_candidate", lesson)
            assert not repeated.isError
            assert repeated.structuredContent["knowledge_id"] == added_result["knowledge_id"]
            assert repeated.structuredContent["idempotent"] is True
            foreign_lesson = {
                **lesson,
                "verification_id": foreign_verifier_result["verification_id"],
                "evidence_refs": [
                    f"verification:{foreign_verifier_result['verification_id']}"
                ],
            }
            rejected_foreign_learning = await session.call_tool(
                "learning_add_candidate", foreign_lesson,
            )
            assert rejected_foreign_learning.isError
            scoped = await session.call_tool("memory_list_candidates", {
                "namespace": added_result["namespace"],
            })
            assert not scoped.isError
            assert "Keep isolated MCP writes" in str(scoped.content)
            corroboration = await session.call_tool("learning_get_corroboration", {
                "knowledge_id": smoke_id,
            })
            assert not corroboration.isError
            assert corroboration.structuredContent["independent_project_count"] == 0

            event = {
                "event_id": "codex-verified-smoke",
                "project_id": active_namespace,
                "task_id": "smoke-1",
                "client_name": "Codex",
                "task_description": "Verify MCP learning event ingestion",
                "outcome": "Tested",
                "completed_at": "2026-10-22T00:00:00Z",
                "status": "task_completed",
                "test_results": {},
            }
            submitted = await session.call_tool("learning_submit_task", {
                "event_json": json.dumps(event),
            })
            assert not submitted.isError
            assert "waiting_for_verification" in str(submitted.content)
            task_status = await session.call_tool("learning_get_status", {
                "event_id": "codex-verified-smoke",
            })
            assert not task_status.isError
            assert "waiting_for_verification" in str(task_status.content)
            pending_candidates = await session.call_tool("memory_list_candidates", {
                "limit": 100,
            })
            assert "MCP smoke" in str(pending_candidates.content)

    anyio.run(_exercise)


def test_verified_mcp_candidate_reaches_hook_only_after_approval(tmp_path):
    """Сквозной путь verifier -> MCP candidate -> approval -> prompt retrieval."""
    database = tmp_path / "end-to-end.sqlite"
    hook_log = tmp_path / "hooks.jsonl"
    project_root = tmp_path / "project"
    project_root.mkdir()
    subprocess.run(
        ["git", "init", "--quiet"], cwd=project_root, check=True,
        stdin=subprocess.DEVNULL, capture_output=True,
    )
    changed_file = project_root / "src" / "retry.py"
    changed_file.parent.mkdir()
    changed_file.write_text("def retry():\n    return 'bounded'\n", encoding="utf-8")
    environment = os.environ.copy()
    environment["AI_MEMORY_DB"] = str(database)
    environment["AI_MEMORY_PROJECT_ROOT"] = str(project_root)
    environment["AI_MEMORY_MAINTAINER"] = "isolated-test-owner"
    environment["AI_MEMORY_HOOK_LOG"] = str(hook_log)
    prompt = "How should transient API retry backoff work?"
    lesson_title = "Use bounded retry backoff for transient API errors"

    verification = subprocess.run(
        [
            sys.executable, "-m", "ai_engineering_memory.cli", "learning", "verify",
            "--task-id", "prompt-retrieval-e2e", "--summary",
            "Verify bounded retry learning flow", "--cwd", str(project_root),
            "--changed-file", "src/retry.py", "--",
            sys.executable, "-c", "assert 'bounded' == 'bounded'",
        ],
        env=environment, capture_output=True, text=True, check=True,
    )
    verifier_result = json.loads(verification.stdout)
    assert verifier_result["process_succeeded"] is True
    assert verifier_result["recorded_changed_files"] == ["src/retry.py"]

    async def _exercise_mcp() -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "ai_engineering_memory.cli", "mcp", "serve"],
            env=environment,
        )
        async with stdio_client(params) as (read_stream, write_stream), ClientSession(
            read_stream, write_stream
        ) as session:
            await session.initialize()
            lesson = {
                "verification_id": verifier_result["verification_id"],
                "title": lesson_title,
                "category": "backend",
                "summary": "Keep transient API retries bounded and observable.",
                "problem": "Transient API failures can cause uncontrolled retry loops.",
                "root_cause": "The retry policy has no upper bound.",
                "solution": "Use bounded backoff and preserve the failure result.",
                "conditions": "Use for API clients that retry transient failures.",
                "limitations": "The verifier proves only that its command exited zero.",
                "verification_summary": "The isolated verifier command exited successfully.",
                "evidence_refs": [
                    f"verification:{verifier_result['verification_id']}",
                    "file:src/retry.py",
                ],
                "confidence": "medium",
                "confidence_basis": "The candidate is tied to one scoped verifier event.",
            }
            added = await session.call_tool("learning_add_candidate", lesson)
            assert not added.isError
            candidate = added.structuredContent
            assert candidate["status"] == "candidate"
            assert candidate["namespace"] == project_namespace(project_root)
            assert candidate["verification_id"] == verifier_result["verification_id"]

            unapproved = await session.call_tool("memory_search", {
                "query": prompt, "status": "approved",
                "namespace": candidate["namespace"],
            })
            assert not unapproved.isError
            assert "Use bounded retry backoff" not in str(unapproved.content)

            approved = await session.call_tool("memory_approve", {
                "knowledge_id": candidate["knowledge_id"],
                "maintainer": "isolated-test-owner",
                "confirmation": f"APPROVE {candidate['knowledge_id']}",
            })
            assert not approved.isError
            assert approved.structuredContent["status"] == "approved"
    anyio.run(_exercise_mcp)
    hook_result = subprocess.run(
        [sys.executable, "-m", "ai_engineering_memory.codex_hooks", "UserPromptSubmit"],
        input=json.dumps({"prompt": prompt, "cwd": str(project_root)}),
        env=environment, capture_output=True, text=True, encoding="utf-8", check=True,
    )
    output = json.loads(hook_result.stdout)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert lesson_title in context
    assert '"scope": "project"' in context
    assert "<untrusted_engineering_memory_json>" in context
    assert len(hook_log.read_text(encoding="utf-8").splitlines()) == 1
