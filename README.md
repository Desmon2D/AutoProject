# AutoProject

Монорепозиторий независимых проектов автоматизации. Каждый проект хранит свои
зависимости, конфигурацию, документацию и команды запуска в отдельном каталоге.

## Проекты

| Проект | Назначение | Документация |
|---|---|---|
| `automation-agent` | Локальная платформа AI-агентов: оркестратор, dashboard, Docker-песочницы и интеграции | [README](projects/automation-agent/README.md) |
| `cron` | Интерфейс и планировщик автоматических AI-задач | [README](projects/cron/README.md) |

## Быстрый старт

Платформа агентов:

```powershell
Set-Location projects\automation-agent
.\scripts\dev\start-low-memory.ps1
```

AI Cron:

```powershell
Set-Location projects\cron
Copy-Item .env.example .env
npm ci
npm run dev:all
```

Команды разработки и переменные окружения описаны в README соответствующего
проекта. Настоящие `.env`-файлы остаются локальными и не должны попадать в Git.

## Структура

```text
AutoProject/
├── projects/
│   ├── automation-agent/
│   └── cron/
├── .gitattributes
├── .gitignore
└── README.md
```

Для новой работы создавайте обычные feature/fix-ветки от `main`; отдельный
проект добавляйте новым каталогом внутри `projects/`.
