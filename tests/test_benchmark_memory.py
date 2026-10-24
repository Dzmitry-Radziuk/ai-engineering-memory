"""Проверяет вспомогательную статистику локального benchmark-набора."""

from ai_engineering_memory import storage
from ai_engineering_memory.settings import Settings
from bench.benchmark_memory import _measure, _percentile, _seed_database


def test_benchmark_percentiles_use_nearest_rank():
    """Процентиль берётся по ближайшему рангу без интерполяции."""
    assert _percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.0
    assert _percentile([1.0, 2.0, 3.0, 4.0], 95) == 4.0


def test_benchmark_measure_includes_warmup_outside_reported_iterations():
    """Прогрев не входит в число измеренных повторов."""
    calls = 0

    def operation():
        nonlocal calls
        calls += 1

    result = _measure(operation, iterations=3)

    assert calls == 4
    assert result["iterations"] == 3
    assert result["p50_ms"] >= 0


def test_benchmark_hook_fixture_distinguishes_empty_and_populated_namespaces(
    tmp_path,
):
    """Глобальные записи не совпадают с Fix только через слово fixture."""
    store = storage.create_storage(Settings(tmp_path / "benchmark.sqlite"))
    with store.activate():
        store.init_database()
        _seed_database(store, 100)
        _seed_database(store, 2, namespace="project:benchmark")

        global_rows = store.search_knowledge(
            "Fix the retry bug in this Python database repository and run tests.",
            namespace="global",
            status="approved",
            prompt_context=True,
            _read_only=True,
        )
        project_rows = store.search_knowledge(
            "Fix the retry bug in this Python database repository and run tests.",
            namespace="project:benchmark",
            status="approved",
            prompt_context=True,
            _read_only=True,
        )

    assert global_rows == []
    assert len(project_rows) == 2
