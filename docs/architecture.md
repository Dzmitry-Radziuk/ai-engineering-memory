# Архитектура

Проект — локальный модульный монолит: CLI и MCP являются адаптерами, а правила и SQLite persistence принадлежат ядру. `storage.py` открывает отдельное SQLite-соединение на операцию, включает foreign keys, busy timeout и WAL. `MigrationEngine` последовательно применяет версии схемы 1–5 отдельными повторяемыми транзакциями; обычный запуск актуальной схемы проверяет версию и обязательные поля и не выполняет DDL или FTS rebuild. Существующие строки и ID сохраняются.

```text
Codex UserPromptSubmit ──> approved search + selective learning instruction
Codex runs verifier ────> local process ──> verification_records + task event
Codex model ────────────> MCP learning_add_candidate
                           validation ──> dedupe ──> project candidate + audit
second verified project ───────────────────────────> global candidate + corroborations
maintainer approval ───────────────────────────────> approved global retrieval
Codex Stop ──────────────> metadata-only hook log; no learning write
CLI ────────────────────> compatibility storage ──> SQLite
MCP knowledge tools ────> MemoryService ──> KnowledgeRepository ──> SQLite adapter
```

Операции с основной базой знаний постепенно переводятся на слои `MemoryService` → `KnowledgeRepository` → `SqliteKnowledgeRepository`. CLI и MCP используют сервис для поиска, обзора, чтения, перечисления и создания кандидатов; интерактивное утверждение знаний в CLI делегируется отдельному `AdminService`. `LearningService` объединяет CLI-сценарии verifier/очереди с repository и workflow ports, а MCP передаёт ему candidate capture, legacy event ingest и scoped status lookup вместе с разрешёнными namespace. `SkillsService` предоставляет обоим адаптерам чтение и создание proposals; `AdminService` выполняет human-only решения после exact confirmation в интерактивном CLI. Отдельные диагностические и backup-команды ещё используют legacy API напрямую, поэтому миграция ядра не завершена. `Settings` выбирает путь к БД, а контекст `Database` позволяет адаптерам изолировать одновременные вызовы с разными базами в одном процессе.

`UserPromptSubmit` выполняет поиск через `MemoryService` с явным `read_only=True`. Если базы ещё нет, поиск возвращает пустой результат и не создаёт файл; hook может отобразить найденные утверждённые записи, но не меняет схему или данные.

Основные таблицы: `knowledge`, `evidence`, `task_events`, `verification_records`, `learning_event_audit`, `learning_corroborations`, `skill_proposals`, `skill_versions`, `schema_migrations`. Старые методы `init_database`, `add_knowledge`, `get_knowledge` и MCP tool names сохранены. Передача `task_verified` через MCP без существующей успешной локальной verification-записи отклоняется.

## Проверка и model-native capture

Для подтверждения global-кандидата каждый проект должен иметь ссылку `file:` на файл из списка изменений, записанного verifier. Одного успешного exit code недостаточно.

`learning verify` запускает явно заданный argv без shell. Он фиксирует тип проверки, task/project IDs, exit code, время, происхождение и SHA-256 команды и output. Сырые аргументы, stdout/stderr, transcript и абсолютный путь в БД не сохраняются. Успешный результат ставится в `pending_analysis`; неуспешный остаётся только как запись попытки проверки.

Модель текущего Codex-сеанса анализирует факты, которые видела в ходе работы. Она вызывает MCP `learning_add_candidate` с точным `verification_id` и кратким структурированным выводом. Hook не запускает модель. Python проверяет существование и успешный exit code записи проверки, получает namespace из проверенного события, валидирует длины/приватные данные/evidence references и сначала атомарно создаёт project candidate. Для межпроектного подтверждения отдельно хранится версионированный SHA-256 fingerprint origin репозитория: разные клоны одного origin считаются одним проектом. Учётные данные, query, исходный URL и пути проектов не сохраняются. Без origin кандидат остаётся проектным; отпечатки старого формата не служат независимым подтверждением. При двух разных актуальных origin fingerprint создаётся отдельный global candidate, связанный с verifier records обоих проектов; последующие fingerprint добавляют подтверждения. Глобальный результат всё ещё candidate; стандартный поиск его не возвращает до отдельного решения maintainer-а.

`learning process` с opt-in localhost Ollama оставлен только для старых событий и выключен по умолчанию; он не нужен для нового пути. Анализ может не состояться: prompt classifier эвристический, модель может не найти урок или не вызвать MCP, проверка может не дать достаточного evidence, а MCP может быть недоступен. Результат модели остаётся кандидатом и не считается доказанной истиной.

## Путь данных и изоляция

Если рядом с импортированным пакетом уже есть `data/memory.db`, используется он; иначе Windows-путь по умолчанию — `%LOCALAPPDATA%/AIEngineeringMemory/memory.db` (на других системах — пользовательский каталог данных). Выбор текущей рабочей папки не переключает базу. Переопределение через `AI_MEMORY_DB` обязано быть абсолютным. Namespace проекта для изоляции получается из нормализованного абсолютного рабочего пути; исходный путь не сохраняется. Отпечаток Git origin используется отдельно только для безопасного межпроектного сравнения, хранится как SHA-256.

FTS5 включается при доступности SQLite; иначе поиск использует параметризованный LIKE. Векторная БД, облачная очередь, фоновый сервис и публикация не входят в текущий объём. Отдельный Ollama/API сервис не требуется.
