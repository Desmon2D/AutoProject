# AutoProject

Локальная платформа для выполнения корпоративных процессов с помощью AI-агентов.
Оркестратор принимает события, выполняет JSON-сценарии как граф шагов и запускает
каждый агентный шаг в отдельной Docker-песочнице.

## Быстрый запуск

Требуются Docker Desktop и PowerShell 7.

```powershell
.\scripts\dev\start-low-memory.ps1
```

Dashboard будет доступен по адресу `http://127.0.0.1:4173`, API оркестратора —
по адресу `http://127.0.0.1:8080`.

Для запуска с Gitea:

```powershell
.\scripts\dev\start-low-memory.ps1 -WithGitea
```

Настройка OpenRouter и smoke-проверка:

```powershell
.\scripts\setup\configure-openrouter.ps1
.\scripts\smoke\openrouter-smoke.ps1 -SkipBuild
```

## Структура репозитория

| Каталог | Назначение |
|---|---|
| `orchestrator/` | FastAPI API, workflow engine, очередь, реестры и управление агентами |
| `sandbox/` | Изолированная среда выполнения агента и runtime-манифесты образов |
| `dashboard/` | Локальная панель состояния и действий над workflow |
| `cron/` | Интерфейс и планировщик автоматических AI-задач |
| `infra/` | Конфигурация локальных инфраструктурных сервисов Plane и SWIRL |
| `scripts/dev/` | Запуск и подготовка локального контура |
| `scripts/setup/` | Настройка провайдеров и секретов разработки |
| `scripts/smoke/` | Сквозные и интеграционные smoke-проверки |
| `docs/` | Архитектура, обзор, планы компонентов и roadmap |

Плагины Harness находятся в `orchestrator/plugins/<name>/`: манифест лежит в
`plugin.json`, реализация — в `source/`. Каталог `orchestrator/image-catalog/`
описывает доступные оркестратору образы, а `sandbox/runtime-manifests/` содержит
манифесты, встраиваемые непосредственно в эти образы.

## Документация

- [Обзор платформы](docs/overview.md)
- [Полная архитектура](docs/architecture.md)
- [Roadmap](docs/roadmap.md)
- [Оркестратор](orchestrator/README.md)
- [Песочница](sandbox/README.md)
- [Dashboard](dashboard/README.md)
- [AI Cron](cron/README.md)

## Проверки

```powershell
Set-Location orchestrator
uv sync --extra dev
uv run pytest -m "not docker"
uv run ruff check .

Set-Location ..\dashboard
npm ci
npm test
npm run lint
```

Docker-проверки требуют запущенный Docker Engine и подготовленные sandbox-образы.
