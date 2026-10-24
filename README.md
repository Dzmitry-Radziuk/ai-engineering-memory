# AI Engineering Memory

Локальная база инженерных знаний для Codex. Она помогает находить уже одобренные решения и сохранять проверенные уроки, чтобы использовать их в следующих задачах. Это долговременная память, а не переобучение модели и не автономный управляющий агент.

Коротко о работе: Codex получает подходящие `approved` знания через hook; новый урок при наличии сначала становится `candidate`; межпроектное подтверждение может создать global candidate; человек решает, что одобрить. Кандидаты не попадают в обычные подсказки, а Skills и проекты автоматически не меняются.

Начните с [простого руководства пользователя](docs/usage.md). Архитектура, безопасность, жизненный цикл знаний и интеграция Codex описаны в [проектной документации](docs/architecture.md), [безопасности](docs/security.md), [цикле знаний](docs/knowledge-lifecycle.md) и [интеграции](docs/codex-integration.md).

## Установка

```powershell
py -3.14 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\ai-memory.exe init
.venv\Scripts\ai-memory.exe status
```

Если рядом с установленным пакетом уже есть `data/memory.db`, используется эта абсолютная база; иначе Windows-путь по умолчанию — `%LOCALAPPDATA%\AIEngineeringMemory\memory.db`. Поэтому открытие другого проекта не переключает базу. `AI_MEMORY_DB` переопределяет путь только если он абсолютный. Существующие целочисленные ID сохраняются при расширении схемы.

Для read-only проверки базы, пользовательской регистрации hooks и последних результатов запустите:

```powershell
.venv\Scripts\ai-memory.exe doctor
.venv\Scripts\ai-memory.exe doctor --json
```

Для настройки hooks Codex сначала проверьте план, затем установите команды в активный профиль:

```powershell
.venv\Scripts\ai-memory.exe hooks install --dry-run
.venv\Scripts\ai-memory.exe hooks install
```

Установщик сохраняет остальные записи `hooks.json`, делает backup перед изменением и не перезаписывает файл при повторном запуске без изменений.

### Резервное копирование базы

Создайте проверенную резервную копию командой `ai-memory backup <путь-к-файлу.sqlite>`. Для восстановления закройте процессы Codex/MCP, которые используют базу, затем запустите `ai-memory restore <путь-к-копии.sqlite>` в интерактивном терминале и введите показанную строку `RESTORE <источник> -> <активная база>`. Перед заменой текущая база автоматически сохраняется рядом с ней как `*.pre-restore-<время>-<идентификатор>.sqlite`. Источник проверяется и при необходимости обновляется до текущей схемы на временной копии.
Dry-run перечисляет изменяемые события и названия полей, но не выводит прежние значения конфигурации.

Команда проверяет целостность существующей SQLite-базы, версию схемы, наличие и количество команд `UserPromptSubmit` и `Stop` в `hooks.json`, предупреждает о повторной регистрации и проверяет доступность указанных исполняемых файлов. Она также показывает краткие метаданные последних записей журнала. Обычный `doctor` не создаёт и не мигрирует базу, не запускает hooks или MCP-сервер и не выводит prompts или ответы. `CODEX_HOME` учитывается при выборе каталога профиля; при его отсутствии используется `%USERPROFILE%\.codex`.

Для дополнительной проверки используйте `.venv\Scripts\ai-memory.exe doctor --probe-mcp`. Probe запускает только распознанную команду AI Engineering Memory, проверяет MCP `initialize` и `tools/list`, затем вызывает read-only `memory_search` на временной пустой БД. Путь БД из конфигурации заменяется только для probe-процесса; основная база не используется. Значения окружения из MCP-конфига передаются дочернему процессу, но не выводятся. Это проверяет запуск и фактический вызов инструмента из процесса `doctor`, но не подтверждает runtime или рабочий каталог MCP внутри Codex и не проверяет содержимое основной базы.

Для результата `Stop` `no_marker_seen` означает только, что маркера не было во входном сообщении; `marker_present_unverified` означает, что маркер был, но сам hook не проверяет запуск или результат команды. Ни один из этих результатов не подтверждает успешное выполнение задачи.

## Память и поиск

Для обзора используйте `memory_overview`: он считает все статусы и namespaces. `memory_search` по умолчанию возвращает только `approved` из `global`; `namespace`, `category` и `status` — отдельные точные фильтры. Codex `UserPromptSubmit` ищет до пяти утверждённых записей из `global` и namespace текущего проекта. Кандидаты в prompt не попадают.

## Проверка и накопление инженерного опыта

`UserPromptSubmit` сохраняет Auto Retrieval и выборочно добавляет короткую инструкцию для содержательных инженерных запросов. После безопасной проверки Codex в том же сеансе решает, есть ли переносимый урок. Если нет — candidate не создаётся. Если есть — модель передаёт структурированный вывод MCP-инструменту `learning_add_candidate`; Python не запускает собственную LLM.

Каждый урок сначала сохраняется как `candidate` своего проекта. Для межпроектного подтверждения сравниваются версионированные хеши нормализованного Git origin: разные копии одного origin не считаются независимыми. Без `origin` урок остаётся проектным — разные локальные пути не доказывают независимость. Исходный URL и учётные данные не сохраняются. После проверок из двух независимых origin система создаёт отдельный `global` candidate с обеими verifier-ссылками. Только maintainer может утвердить глобального кандидата; обычный поиск и prompt hook используют только `approved` записи. Активные Skills автоматически не меняются.

Для фиксации реального результата Codex запускает фактическую команду через verifier:

```powershell
.venv\Scripts\ai-memory.exe learning verify --task-id task-123 --summary "Harden retry handling" --cwd D:\Projects\sample --changed-file src\retry.py -- python -m pytest -q
.venv\Scripts\ai-memory.exe learning status
.venv\Scripts\ai-memory.exe learning list
.venv\Scripts\ai-memory.exe learning attempts
```

`learning attempts` показывает последние неудачные запуски проверок без сырых команд и вывода; добавьте `--all`, чтобы включить успешные.
`learning status` дополнительно показывает общий счётчик проверок, число неудачных запусков и число ошибок запуска команды отдельно от очереди анализа.

Verifier сохраняет только переданные пути, которые `git status` считает изменёнными, staged или новыми; clean-файлы и пути вне Git не становятся `file:`-evidence. Принятые пути видны в поле `recorded_changed_files`.

Передавай `--changed-file` только для файлов текущей задачи. Verifier запишет лишь пути, которые Git показывает изменёнными, staged или новыми; принятый список вернётся в `recorded_changed_files`. Для межпроектного подтверждения candidate укажи записанный путь как `file:<relative-path>`; посторонние файлы не включай.

`learning status` distinguishes the Codex-native, best-effort capture workflow from the optional legacy background analyzer. `automatic_analysis` and `background_analysis_configured` describe only that background analyzer; use `doctor` to inspect hook registration and recent executions.
When the optional Ollama provider is configured, `provider_configured` and `legacy_manual_analysis_available` report that it can be run with the explicit `learning process` command; they do not mean a background worker is running.

CLI запускает argv напрямую без shell, проверяет код завершения и сохраняет только digest команды/вывода и метаданные происхождения. Сырые аргументы, stdout/stderr, transcript и абсолютный cwd не сохраняются. Ответ Codex отдельно валидируется: server требует точный успешный `verification_id`, выводит namespace из события и создаёт только project-scoped `candidate`.

Чтобы просмотреть кандидатов общей памяти и текущего проекта, запустите интерактивный обзор:

```powershell
.venv\Scripts\ai-memory.exe knowledge review --cwd D:\Projects\sample
```

Для утверждения или отклонения нужно ввести точное подтверждение; отклонение также требует причины. Решения журналируются. В неинтерактивном запуске обзор отказывается менять статусы.

Отдельные human-only действия доступны через `admin`; перед выполнением CLI требует ввести точную фразу с ID или версией:

```powershell
.venv\Scripts\ai-memory.exe admin knowledge approve 12
.venv\Scripts\ai-memory.exe admin knowledge reject 12 --reason "Insufficient evidence"
.venv\Scripts\ai-memory.exe admin skill approve <proposal-id>
.venv\Scripts\ai-memory.exe admin skill reject <proposal-id> --reason "Out of scope"
.venv\Scripts\ai-memory.exe admin skill rollback sample-skill 1.0.0
```

Решения по Skills требуют настроенного `AI_MEMORY_MAINTAINER`; это локальная проверка конфигурации, не механизм аутентификации. Admin-команды доступны только в интерактивном терминале и отсутствуют в MCP.

MCP ограничивает размеры полей, отклоняет секреты и непроверенные evidence-ссылки, ищет точные/похожие `approved` и `candidate` в `global` и точном проекте, записывает provenance и audit. Повтор той же проверки идемпотентен. Значимый урок — оценка Codex, а не доказанный сервером факт; успешный exit code подтверждает только завершение выбранного процесса.
В candidate сервер отдельно показывает этот факт и помечает описание проверки от Codex как непроверенное: verifier не оценивает, насколько выбранная команда относится к задаче или полно покрывает изменения.

Legacy `learning process` с opt-in localhost Ollama оставлен для старых ожидающих событий. Он выключен по умолчанию и не нужен в обычном рабочем цикле Codex. Никакой внешний LLM/API не используется.

Ошибочное предложение Learning Engine можно отклонить с сохранением истории:

```powershell
.venv\Scripts\ai-memory.exe learning withdraw 12 --reason "Duplicate finding"
```

## Codex, MCP и Skills

MCP использует stdio. Инструкции по настройке и ограничениям находятся в [docs/codex-integration.md](docs/codex-integration.md). Репозиторий содержит marketplace и Skill-плагин `AI Engineering Memory`; текущая локальная версия manifest — `1.5.9`. Обновление manifest не меняет уже установленную копию плагина: для фактической загрузки этой версии её нужно отдельно обновить в Codex.

Диагностический JSONL hooks содержит timestamp, событие, исход, длительность, счётчики, тип ошибки и версию hook-а; prompt, ответы и tool output не записываются. `ai-memory doctor --json` сравнивает ожидаемую версию с версией в последней записи каждого события. Несовпадение говорит только о том, что последняя записанная версия была старой; без нового запуска hook-а оно не доказывает, что текущая конфигурация или установленный код устарели. Утверждённая память передаётся как ограниченный JSON-блок недоверенных справочных данных. Stop остаётся диагностическим и не создаёт verified events. Инструкция hook-а — best-effort: эвристика может пропустить задачу, Codex может не последовать инструкции, а MCP может быть недоступен.

## Проверки

GitHub Actions запускает Ruff и полный набор тестов на Ubuntu и Windows с Python
3.12, 3.13 и 3.14, а также собирает wheel и исходный архив и проверяет установленную
команду CLI. Проверки используют временные пути runner, не подключаясь к пользовательской базе знаний. Workflow
запускается при push в `main`, pull request или вручную.

```powershell
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m pytest -v --basetemp="$env:TEMP\ai-memory-pytest"
```

Тесты используют отдельные временные SQLite-файлы. Основная база не нужна для тестового прогона.

## Ограничения

- `learning verify` подтверждает запуск указанного процесса и его exit code; событие не помечает произвольную команду как пройденные тесты и не оценивает полноту проверки продукта.
- Candidate capture поручен модели текущего Codex-сеанса; автоматическое соблюдение инструкции не гарантируется.
- Одно событие создаёт кандидат только в namespace соответствующего проекта. Автоматический вывод глобальных правил не выполняется.
- MCP доступен локальному пользователю через stdio; инструменты записи создают кандидаты и learning-события, а утверждение, отклонение и откат знаний/Skills через MCP недоступны.
- Нет фонового воркера, облачного LLM, Git Sync, векторного поиска или публикации.
