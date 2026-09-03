# DIT Outlook MCP

Локальный MCP-сервер только для чтения корпоративной почты и календаря через
Exchange Web Services (EWS) и NTLM.

## Возможности

- `search_mail` — поиск писем;
- `read_mail` — чтение письма;
- `resolve_people` — поиск сотрудников и адресов;
- `read_calendar` — чтение календаря;
- `find_meeting_times` — поиск свободного времени без создания встречи.

Создание черновиков, отправка писем, создание встреч и изменение данных не
регистрируются как MCP-инструменты.

## Установка

Из корня проекта:

```powershell
.\services\dit-outlook-mcp\install.ps1
```

Outlook MCP использует отдельное виртуальное окружение, потому что закреплённая
версия исходного Exchange MCP требует `mcp<2`, а остальные корпоративные MCP
используют `mcp==2.0.0`.

Зависимость загружается из официального репозитория
[`ShermanGu/exchange-ews-mcp`](https://github.com/ShermanGu/exchange-ews-mcp),
закреплена на commit `859b275db83184c9125ae50551c8d0fe89ad1c39` и проверяется по SHA-256.
Исходный проект распространяется по лицензии MIT.

## Авторизация

```powershell
.\services\dit-outlook-mcp\.venv\Scripts\exchange-ews-mcp.exe configure
```

Укажите корпоративный EWS endpoint вида
`https://owa.mos.ru/EWS/Exchange.asmx` и имя пользователя. Пароль сохраняется в
Windows Credential Manager, а не в репозитории или YAML.

## Подключение

- OpenWebUI использует [`../../infra/local-ai/mcp-servers.json`](../../infra/local-ai/mcp-servers.json).
- Для DIT Agent скопируйте блок из [`config.example.yaml`](config.example.yaml)
  в `mcp_servers` изолированного профиля и создайте новую сессию.
