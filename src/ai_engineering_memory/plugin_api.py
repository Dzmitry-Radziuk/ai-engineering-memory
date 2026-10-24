"""Версионированные контракты расширений AI Engineering Memory."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

PLUGIN_API_VERSION = 1


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """Описывает запрос к подключаемому backend поиска."""

    text: str
    namespace: str | None
    status: str | None = "approved"
    category: str | None = None
    limit: int = 10
    prompt_context: bool = False
    read_only: bool = False


@dataclass(frozen=True, slots=True)
class KnowledgeRecord:
    """Представляет запись для передачи exporter расширения."""

    id: int
    uuid: str
    namespace: str
    category: str
    title: str
    content: str
    status: str
    confidence: float


@dataclass(frozen=True, slots=True)
class SearchHit:
    """Передаёт ядру только ID кандидата на выдачу и необязательный score."""

    knowledge_id: int
    score: float = 0.0


@dataclass(frozen=True, slots=True)
class AnalysisRequest:
    """Содержит очищенный ввод для необязательного analyzer расширения."""

    project_id: str
    summary: str
    changed_files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CandidateProposal:
    """Представляет непроверенный вывод, который нельзя утверждать автоматически."""

    title: str
    category: str
    content: str
    rationale: str


class SearchBackend(Protocol):
    """Контракт поиска, который возвращает ID для повторной проверки ядром."""

    plugin_api_version: int

    def search(self, query: SearchQuery) -> Sequence[SearchHit]: ...


class AnalyzerProvider(Protocol):
    """Контракт анализатора, возвращающего только неподтверждённое предложение."""

    plugin_api_version: int

    def analyze(self, request: AnalysisRequest) -> CandidateProposal | None: ...


class ImporterProvider(Protocol):
    """Контракт преобразования внешних файлов в непроверенные предложения."""

    plugin_api_version: int

    def import_records(self, source: Path) -> Iterable[CandidateProposal]: ...


class ExporterProvider(Protocol):
    """Контракт экспорта явно переданных записей в выбранный файл."""

    plugin_api_version: int

    def export_records(
        self,
        records: Sequence[KnowledgeRecord],
        destination: Path,
    ) -> None: ...
