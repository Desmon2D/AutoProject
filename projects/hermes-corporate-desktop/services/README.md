# Корпоративные MCP-сервисы

Интеграции вынесены из ядра Hermes в независимые MCP-процессы. Все серверы в
этом репозитории предоставляют только операции чтения; права фактического
доступа дополнительно ограничиваются учётной записью пользователя и политиками
корпоративного сервиса.

| Сервис | Источник | Авторизация | Код |
| --- | --- | --- | --- |
| DIT Git | GitLab REST API v4 | Personal Access Token в `DIT_GIT_TOKEN` | [`dit-git-mcp`](dit-git-mcp/) |
| DIT Outlook | Exchange EWS + NTLM | пароль в Windows Credential Manager | [`dit-outlook-mcp`](dit-outlook-mcp/) |
| DIT Staff | Mirapolis web/API | локальная сессия или учётные данные в Windows Credential Manager | [`dit-staff-mcp`](dit-staff-mcp/) |
| DIT CFC | корпоративный web/API | cookies браузерной сессии в Windows Credential Manager | [`dit-cfc-mcp`](dit-cfc-mcp/) |
| DIT Jira | Jira Server/Data Center REST API | PAT или Basic Auth; по умолчанию PAT в `DIT_JIRA_TOKEN` | [`dit-jira-mcp`](dit-jira-mcp/) |
| DIT Confluence | Confluence Server/Data Center REST API | PAT или Basic Auth; по умолчанию PAT в `DIT_CONFLUENCE_TOKEN` | [`dit-confluence-mcp`](dit-confluence-mcp/) |

## Установка серверов из репозитория

Из корня проекта, после создания `.venv`:

```powershell
uv pip install --python .venv\Scripts\python.exe `
  -e services\dit-git-mcp `
  -e services\dit-staff-mcp `
  -e services\dit-cfc-mcp `
  -e services\dit-jira-mcp `
  -e services\dit-confluence-mcp
```

Outlook MCP устанавливается отдельно из-за несовместимой основной версии
библиотеки `mcp`:

```powershell
.\services\dit-outlook-mcp\install.ps1
```

Конкретные переменные, браузерная авторизация и MCP-конфигурация описаны в
README каждого сервиса. Не добавляйте токены, пароли, cookies, пользовательские
профили браузеров или корпоративные ответы API в Git.
