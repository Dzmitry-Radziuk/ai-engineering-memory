"""Проверяет локальный analyzer без сетевых соединений."""

from __future__ import annotations

import json
from urllib.error import URLError

import pytest

from ai_engineering_memory import learning


def _analysis_result():
    """Возвращает допустимый структурированный ответ для verifier evidence."""
    return {
        "should_store": True,
        "title": "Bounded retry state",
        "lesson": "Keep retries bounded and preserve the final error state.",
        "category": "backend",
        "applicability": "Use for network calls with transient failures.",
        "rationale": "A trusted local test confirmed the behavior.",
        "evidence_refs": ["verification:verify-1"],
        "limitations": "Only applies when retries are safe to repeat.",
        "confidence": "medium",
        "confidence_basis": "One successful local verification supports this scope.",
    }


class _Response:
    """Implements only the response context and bounded read used by provider."""

    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:11434/api/generate",
        "http://example.com:11434/api/generate",
        "http://user:password@127.0.0.1:11434/api/generate",
        "http://127.0.0.1/api/generate",
    ],
)
def test_ollama_provider_rejects_invalid_endpoint(endpoint):
    """Поддерживается только локальный HTTP endpoint с явным портом."""
    with pytest.raises(learning.ProviderError, match="InvalidLocalProviderConfig"):
        learning.OllamaProvider("local-model", endpoint)


def test_ollama_provider_sends_bounded_untrusted_json_only(monkeypatch):
    """Provider отправляет JSON на loopback без системных proxy и raw stdout."""
    expected = _analysis_result()

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "http://127.0.0.1:11434/api/generate"
            assert timeout == 3
            body = json.loads(request.data)
            assert body["stream"] is False
            assert body["format"] == "json"
            assert "untrusted" in body["prompt"].casefold()
            return _Response(
                json.dumps({"response": json.dumps(expected)}).encode("utf-8")
            )

    monkeypatch.setattr(learning, "build_opener", lambda handler: Opener())
    provider = learning.OllamaProvider(
        " local-model ", "http://127.0.0.1:11434/api/generate", timeout=3
    )

    assert provider.analyze({"task_summary": "short verified task"}) == expected
    assert provider.model == "local-model"


def test_ollama_provider_rejects_oversized_input_response_and_bad_json(monkeypatch):
    """Размер и формат локального ответа ограничены до разбора результата."""
    provider = learning.OllamaProvider("model", "http://localhost:11434/api/generate")
    with pytest.raises(learning.ProviderError, match="InputTooLarge"):
        provider.analyze({"text": "x" * 5_000})

    class Opener:
        def __init__(self, body):
            self.body = body

        def open(self, request, timeout):
            return _Response(self.body)

    monkeypatch.setattr(
        learning,
        "build_opener",
        lambda handler: Opener(b"x" * (learning.MAX_PROVIDER_RESPONSE + 1)),
    )
    with pytest.raises(learning.ProviderError, match="ProviderResponseTooLarge"):
        provider.analyze({"task_summary": "safe"})

    monkeypatch.setattr(learning, "build_opener", lambda handler: Opener(b"not json"))
    with pytest.raises(learning.ProviderError, match="InvalidProviderJSON"):
        provider.analyze({"task_summary": "safe"})

    monkeypatch.setattr(
        learning,
        "build_opener",
        lambda handler: Opener(b'{"response":"[]"}'),
    )
    with pytest.raises(learning.ProviderError, match="InvalidProviderResult"):
        provider.analyze({"task_summary": "safe"})


def test_ollama_provider_redacts_transport_failures_and_config_is_opt_in(
    monkeypatch,
):
    """Provider не обращается в сеть без явного model и не раскрывает URL ошибки."""
    monkeypatch.delenv("AI_MEMORY_OLLAMA_MODEL", raising=False)
    assert learning.configured_provider() is None

    monkeypatch.setenv("AI_MEMORY_OLLAMA_MODEL", "local-model")
    monkeypatch.setenv("AI_MEMORY_OLLAMA_URL", "http://192.0.2.5:11434/api/generate")
    with pytest.raises(learning.ProviderError, match="InvalidLocalProviderConfig"):
        learning.configured_provider()

    provider = learning.OllamaProvider("local-model", "http://127.0.0.1:11434/api")

    class FailingOpener:
        def open(self, request, timeout):
            raise URLError("private host and response must not leak")

    monkeypatch.setattr(learning, "build_opener", lambda handler: FailingOpener())
    with pytest.raises(
        learning.ProviderError, match="LocalProviderUnavailable"
    ) as error:
        provider.analyze({"task_summary": "safe"})
    assert "private host" not in str(error.value)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("should_store", "yes", "InvalidAnalysisSchema"),
        ("title", "", "InvalidAnalysisField"),
        ("category", "Bad-Category", "InvalidAnalysisCategory"),
        ("confidence", "certain", "InvalidConfidenceLevel"),
        ("evidence_refs", ["verification:other"], "EvidenceReferenceMismatch"),
        ("lesson", "token=private-value", "SensitiveAnalysisRejected"),
        ("lesson", "short tip", "TrivialAnalysisRejected"),
    ],
)
def test_analysis_validator_rejects_untrusted_or_unsupported_results(
    field, value, message
):
    """Analysis validation enforces schema, evidence, privacy and usefulness."""
    result = _analysis_result()
    result[field] = value

    with pytest.raises(learning.ProviderError, match=message):
        learning._validate_analysis(result, "verify-1")


def test_analysis_validator_allows_explicit_no_insight_without_lesson_quality_gate():
    """Явный no-insight не требует минимального количества слов в уроке."""
    result = _analysis_result()
    result["should_store"] = False
    result["lesson"] = "No lesson"

    assert learning._validate_analysis(result, "verify-1")["should_store"] is False


def test_process_pending_records_untrusted_verification_failure(monkeypatch):
    """Очередь не передаёт событие analyzer без доверенной проверки."""
    event = {
        "event_id": "event-1",
        "verification_id": "verify-1",
        "project_id": "project:0123456789abcdef",
        "task_description": "safe summary",
        "changed_files": "[]",
    }
    pending = iter((event, None))
    monkeypatch.setattr(learning.storage, "claim_learning_event", lambda: next(pending))
    monkeypatch.setattr(learning.storage, "get_verification", lambda _id: None)
    failures = []
    monkeypatch.setattr(
        learning.storage,
        "fail_learning_event",
        lambda event_id, **kwargs: (
            failures.append((event_id, kwargs)) or "retryable_error"
        ),
    )
    provider = type("Provider", (), {"analyze": lambda self, _payload: pytest.fail()})()

    result = learning.process_pending(provider=provider)

    assert result["outcomes"] == [{"event_id": "event-1", "status": "retryable_error"}]
    assert failures[0][1]["error_type"] == "MissingTrustedVerification"


@pytest.mark.parametrize(
    ("analysis", "similar", "candidate", "expected"),
    [
        ({"should_store": False}, False, None, "no_insight"),
        (_analysis_result(), True, None, "duplicate"),
        (_analysis_result(), False, None, "duplicate"),
        (_analysis_result(), False, {"id": 91}, "candidate_created"),
    ],
)
def test_process_pending_routes_analysis_to_candidate_or_no_insight(
    monkeypatch, analysis, similar, candidate, expected
):
    """Очередь отделяет no-insight и дубли от новых project candidates."""
    no_insight = {"should_store": False}
    if analysis == no_insight:
        analysis = _analysis_result()
        analysis["should_store"] = False
    else:
        analysis = dict(analysis)
    analysis["evidence_refs"] = ["verification:verify-2"]
    event = {
        "event_id": "event-2",
        "verification_id": "verify-2",
        "project_id": "project:0123456789abcdef",
        "task_description": "safe summary",
        "changed_files": "[]",
    }
    verification = {
        "verification_id": "verify-2",
        "verification_type": "command",
        "exit_code": 0,
        "artifact_digest": "digest",
        "trust_status": "trusted_local_process",
    }
    pending = iter((event, None))
    monkeypatch.setattr(learning.storage, "claim_learning_event", lambda: next(pending))
    monkeypatch.setattr(learning.storage, "get_verification", lambda _id: verification)
    monkeypatch.setattr(learning.storage, "list_knowledge", lambda **_kwargs: [])
    monkeypatch.setattr(learning, "_similar_existing", lambda *_args: similar)
    monkeypatch.setattr(
        learning.storage,
        "create_learning_candidate",
        lambda *_args, **_kwargs: candidate,
    )
    finished = []
    monkeypatch.setattr(
        learning.storage,
        "finish_learning_event",
        lambda event_id, **kwargs: finished.append((event_id, kwargs)),
    )
    provider = type("Provider", (), {"analyze": lambda self, _payload: analysis})()

    result = learning.process_pending(provider=provider)

    assert result["outcomes"][0]["status"] == expected
    if expected in {"no_insight", "duplicate"}:
        assert finished
    if expected == "candidate_created":
        assert result["outcomes"][0]["knowledge_id"] == 91


def test_process_pending_records_provider_failure_without_sensitive_message(
    monkeypatch,
):
    """Неожиданная ошибка analyzer попадает в очередь только как тип исключения."""
    event = {
        "event_id": "event-3",
        "verification_id": "verify-3",
        "project_id": "project:0123456789abcdef",
        "task_description": "safe summary",
        "changed_files": "[]",
    }
    verification = {
        "verification_id": "verify-3",
        "verification_type": "command",
        "exit_code": 0,
        "artifact_digest": "digest",
        "trust_status": "trusted_local_process",
    }
    pending = iter((event, None))
    monkeypatch.setattr(learning.storage, "claim_learning_event", lambda: next(pending))
    monkeypatch.setattr(learning.storage, "get_verification", lambda _id: verification)
    monkeypatch.setattr(
        learning.storage,
        "fail_learning_event",
        lambda _id, **_kwargs: "retryable_error",
    )

    class FailingProvider:
        def analyze(self, _payload):
            raise RuntimeError("private provider response must not be persisted")

    result = learning.process_pending(provider=FailingProvider())

    assert result["outcomes"][0] == {
        "event_id": "event-3",
        "status": "retryable_error",
        "error_type": "RuntimeError",
    }
