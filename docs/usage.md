 Как пользоваться AI Engineering Memory

## Что это такое

AI Engineering Memory — локальная база инженерных заметок для Codex. Она помогает повторно использовать найденные решения и ограничения между задачами и проектами. Это память, а не обучение весов модели: система сохраняет знания в SQLite, а Codex читает подходящие одобренные записи.

Система не управляет проектами сама. Она не меняет код, настройки проекта или Skills без отдельного действия человека. Новые заметки сначала становятся кандидатами; обычный поиск и автоматический контекст их не показывают.

## Обычный сценарий

1. Codex получает задачу. `UserPromptSubmit` может добавить подходящие одобренные знания из общей базы и текущего проекта.
2. Codex изучает и меняет только тот проект, над которым работает пользователь.
3. Для подходящей инженерной задачи Codex запускает проверку через `ai-memory learning verify`. Команда сохраняет результат процесса и контрольные суммы, а не исходный вывод.
4. Если из работы есть повторно полезный урок, Codex может предложить его как project candidate. Если переносимого урока нет, запись не создаётся.
5. Похожие уроки с подтверждением из двух Git origin могут дать отдельный global candidate. Два клона одного origin считаются одним источником. Без `origin` урок остаётся в проекте.
6. Maintainer просматривает кандидатов и вручную одобряет, отклоняет или оставляет их. Только одобренные знания доступны обычному поиску.

`Stop` — диагностический hook. Маркер `AI_MEMORY_TASK_VERIFIED` сам по себе ничего не проверяет и не создаёт событие обучения.

## Установка и локальная проверка

В PowerShell из корня репозитория:

```powershell
py -3.14 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\ai-memory.exe init
.venv\Scripts\ai-memory.exe status
.venv\Scripts\ai-memory.exe doctor
```

Путь базы по умолчанию — `%LOCALAPPDATA%\AIEngineeringMemory\memory.db`; `AI_MEMORY_DB` может указать другой абсолютный путь. Команда `doctor` только читает конфигурацию и состояние. Она не запускает hook/MCP, не создаёт базу и не меняет настройки.

Проверки репозитория, работающие с временными базами:

```powershell
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m pytest -v --basetemp="$env:TEMP\ai-memory-pytest"
```

Тесты создают искусственные Git-проекты и временные SQLite-файлы. Они проверяют изоляцию, verifier, поведение отпечатков, межпроектное подтверждение, MCP-протокол и диагностику; пользовательская база для них не нужна.

## Проверка на искусственном проекте

Эта проверка создаёт только временную папку и временную базу. Она не трогает ваши проекты, профиль Codex или основную базу.

```powershell
$mockId = [guid]::NewGuid().ToString("N")
$mockRoot = Join-Path $env:TEMP "aem-mock-project-$mockId"
$mockDb = Join-Path $env:TEMP "aem-mock-memory-$mockId.db"
$pythonExe = (Resolve-Path ".venv\Scripts\python.exe").Path
$previousDb = $env:AI_MEMORY_DB
$previousProject = $env:AI_MEMORY_PROJECT_ROOT
$env:AI_MEMORY_DB = $mockDb
$env:AI_MEMORY_PROJECT_ROOT = $mockRoot

try {
    New-Item -ItemType Directory -Path $mockRoot | Out-Null
    .venv\Scripts\ai-memory.exe init
    if ($LASTEXITCODE -ne 0) { throw "init failed" }
    .venv\Scripts\ai-memory.exe learning verify --task-id mock-task --summary "Проверка временного проекта" --cwd $mockRoot -- $pythonExe -c "print('mock check passed')"
    if ($LASTEXITCODE -ne 0) { throw "verification failed" }
    .venv\Scripts\ai-memory.exe learning status
    if ($LASTEXITCODE -ne 0) { throw "status failed" }
    .venv\Scripts\ai-memory.exe learning attempts
    if ($LASTEXITCODE -ne 0) { throw "attempts failed" }
}
finally {
    $env:AI_MEMORY_DB = $previousDb
    $env:AI_MEMORY_PROJECT_ROOT = $previousProject
    if (Test-Path -LiteralPath $mockRoot) { Remove-Item -LiteralPath $mockRoot -Recurse -Force }
    foreach ($path in @($mockDb, "$mockDb-wal", "$mockDb-shm")) {
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
    }
}
```

Успешный результат означает только, что указанный процесс завершился с кодом `0`; событие попадёт в `pending_analysis`, но кандидат автоматически не появится. Это не доказывает корректность продукта или достаточность проверки. Пример восстанавливает прежние переменные окружения и удаляет созданные временные файлы даже при ошибке. Для полной автоматизированной проверки репозитория используйте команды pytest и Ruff выше.

## Подключение Codex

Подключение hooks и MCP меняет пользовательский профиль Codex и поэтому выполняется отдельно от тестов репозитория. Сначала ознакомьтесь с [инструкцией интеграции](codex-integration.md). Перед любой установкой проверьте план:

```powershell
.venv\Scripts\ai-memory.exe hooks install --dry-run
```

Только если вы решили подключить hooks, команда без `--dry-run` изменяет user-level `hooks.json` и сохраняет backup. После подключения перезапустите Codex и проверьте свежие события командой `ai-memory doctor --json`. `doctor --probe-mcp` — отдельный необязательный запуск сервера: после handshake и списка инструментов он вызывает только read-only `memory_search` на временной пустой БД. Probe проверяет доступность вызова из `doctor`, но не доказывает запуск MCP внутри Codex или доступность основной БД.

### Backup и восстановление базы

`ai-memory backup <путь-к-файлу.sqlite>` создаёт и проверяет согласованную копию SQLite. Для восстановления сначала остановите процессы Codex/MCP, работающие с этой базой, затем выполните `ai-memory restore <путь-к-копии.sqlite>` в интерактивном терминале. CLI попросит ввести точную строку вида `RESTORE <абсолютный путь источника> -> <абсолютный путь активной базы>`. Неверный ответ или запуск без TTY отменяет операцию. Перед заменой текущая база сохраняется в соседний файл `*.pre-restore-<время>-<идентификатор>.sqlite`; исходная копия не изменяется. Для проверки источника и миграции используется временный файл рядом с активной базой.

### Отдельная область MCP для каждого проекта

Если глобальная настройка MCP закрепляет один корень, задайте `AI_MEMORY_PROJECT_ROOT` отдельно для каждого доверенного проекта. Команда ниже только печатает TOML; она не создаёт `.codex/config.toml` и не меняет другие файлы:

```powershell
.venv\Scripts\ai-memory.exe project-config --cwd D:\Projects\sample
```

Добавьте напечатанные таблицы в `D:\Projects\sample\.codex\config.toml`. Если файл уже есть, объедините таблицы с существующими настройками, не заменяя весь файл; сохраните нужные переменные из его `env`. Codex загружает проектную конфигурацию MCP только для доверенного проекта; см. [документацию Codex о проектной конфигурации](https://learn.chatgpt.com/docs/config-file/config-reference). Вывод команды использует Python, из которого запущен `ai-memory`; если переместите или переустановите AEM, сгенерируйте блок заново. Проверка `doctor` по user-level профилю не подтверждает фактическую загрузку проектного MCP внутри Codex.

Проверить объявленное объединение пользовательского и проектного MCP-блока можно без записи файлов: `.venv\Scripts\ai-memory.exe doctor --cwd D:\Projects\sample\src\feature --json`. `doctor` учитывает `.codex/config.toml` от Git-корня до переданного каталога, включая вложенные переопределения. Это read-only проверка TOML, но она не подтверждает, что Codex доверяет проекту или уже загрузил этот MCP-сервер.

## Обзор и решение по кандидатам

Для проекта явно укажите его корень:

```powershell
.venv\Scripts\ai-memory.exe knowledge review --cwd D:\Projects\sample
```

Интерактивный обзор показывает кандидатов общей памяти и указанного проекта с происхождением и проверками. Он требует точного подтверждения для смены статуса. Отклонение требует причины. Просматривайте, утверждайте или отклоняйте только записи, которые проверили сами.

Подробные сведения о компонентах, защите данных и жизненном цикле знаний: [архитектура](architecture.md), [безопасность](security.md), [жизненный цикл знаний](knowledge-lifecycle.md), [Codex, MCP и hooks](codex-integration.md).
