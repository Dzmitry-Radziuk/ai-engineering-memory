# Разработка расширений

AI Engineering Memory публикует типизированный SPI в `ai_engineering_memory.plugin_api`.
Версия контрактов задаётся `PLUGIN_API_VERSION`; текущая версия — `1`. Python package
entry points обнаруживаются через `PluginRegistry`, но ядро не сканирует и не загружает
расширения при старте. Загрузить можно только plugin, выбранный явным вызовом `load()`.
Штатный retrieval использует SQLite/FTS5. Search backend можно явно выбрать для процесса
через `AI_MEMORY_SEARCH_PLUGIN=<entry-point-name>`; analyzer включается отдельно через `AI_MEMORY_ANALYZER_PLUGIN=<entry-point-name>`.

Поддерживаемые entry-point группы:

| Группа | Контракт | Назначение |
|---|---|---|
| `ai_engineering_memory.search` | `SearchBackend` | Поиск по `SearchQuery`, возврат ID через `SearchHit` |
| `ai_engineering_memory.analyzers` | `AnalyzerProvider` | Анализ `AnalysisRequest`, возврат `CandidateProposal` |
| `ai_engineering_memory.importers` | `ImporterProvider` | Преобразование файла в кандидаты |
| `ai_engineering_memory.exporters` | `ExporterProvider` | Экспорт переданного набора записей |

Пример регистрации search backend в `pyproject.toml` стороннего пакета:

```toml
[project.entry-points."ai_engineering_memory.search"]
my_backend = "my_memory_plugin.backend:create_backend"
```

После установки выбранного пакета задайте его имя процессу AEM. В Codex это можно
добавить к существующей env-таблице MCP, не заменяя остальную конфигурацию:

```toml
[mcp_servers.ai_engineering_memory.env]
AI_MEMORY_SEARCH_PLUGIN = "my_backend"
```

Для command hooks переменная должна быть доступна у процесса Codex, который запускает hook.
Если переменная не задана, используется штатный SQLite/FTS5 поиск.

Entry point должен указывать на фабрику без аргументов. Она возвращает объект с целым
`plugin_api_version = 1` и методом соответствующей группы. Контракты и модели находятся
в `plugin_api.py`. Search backend получает namespace/status/category/limit и возвращает
только ID и score. Ядро повторно читает канонические строки из SQLite в read-only режиме
и заново применяет namespace, status, category и limit; поля записи из plugin не принимаются.
Так backend может влиять на ранжирование, но не может выдать чужой namespace или candidate
вместо approved.

Просмотреть имена можно через `PluginRegistry.available(group)`; этот вызов не импортирует
код расширений. Затем вызывающая сторона явно выбирает provider через
`PluginRegistry.load(group, name)`. Для штатного поиска переменная `AI_MEMORY_SEARCH_PLUGIN`
выбирает entry point из `ai_engineering_memory.search`. Analyzer можно явно подключить к
очереди через `AI_MEMORY_ANALYZER_PLUGIN`; importer/exporter пока доступны только как SPI.

Ошибка выбранного расширения становится `PluginLoadError`, содержащей имя plugin и класс
ошибки без произвольного текста исходного исключения. Несовместимая версия или отсутствующий
метод отклоняются. Невыбранные расширения не импортируются; отсутствие или ошибка plugin
не влияет на обычный запуск ядра, поскольку registry не включён в автоматический bootstrap.

Entry point — доверенный установленный Python-код, а не песочница. Перед выбором расширения
проверьте его происхождение и права. Сам факт установки, обнаружения или загрузки plugin не
утверждает созданные им знания. `CandidateProposal` всегда остаётся неподтверждённым выводом.

Analyzer вызывается только для ожидающих событий с успешной доверенной локальной проверкой. Он получает краткое описание задачи и обезличенный namespace проекта; ядро само привязывает точный verification evidence, проверяет структуру, размер, чувствительные данные и дубликаты. Предложение записывается исключительно как `candidate` и требует отдельного решения человека. Без `AI_MEMORY_ANALYZER_PLUGIN` analyzer-плагины не загружаются, а настроенный legacy-провайдер Ollama продолжает работать как прежде.

SQLite/FTS5 остаётся выбором по умолчанию. Importer/exporter расширения пока не подключены к штатному workflow; их контракты доступны для разработки интеграций.
