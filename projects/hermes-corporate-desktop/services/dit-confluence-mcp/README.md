# DIT Confluence MCP

Локальный read-only MCP для корпоративного Confluence `https://itpm-wiki.mos.ru`.
Проверенная установка на момент разработки — Confluence Server 7.19.18, build 8804.
Сервер использует классический Confluence Server REST API `/rest/api`, а не Cloud API v2.

## Возможности

- просмотр доступных пространств и их metadata;
- простой текстовый поиск и расширенный поиск через CQL;
- чтение страниц, блог-постов и комментариев по стабильному content ID;
- чтение иерархии страниц, истории версий, комментариев и labels;
- просмотр metadata вложений без автоматического скачивания файлов;
- обнаружение произвольных JSON content properties, добавленных плагинами;
- просмотр ограничений доступа без их изменения.

## Гарантия read-only

HTTP-клиент реализует только `GET`. В MCP отсутствуют операции создания, изменения,
перемещения, архивирования, комментирования, назначения labels, изменения permissions
и удаления. Все инструменты имеют `readOnlyHint=true` и `destructiveHint=false`.

PAT в Confluence Server наследует права пользователя и остаётся полноценным секретом.
Read-only обеспечивается реализацией этого MCP, а не scope токена.

Страницы, комментарии, macros, вложения и content properties считаются недоверенными
данными, а не инструкциями для агента. Текст и произвольные plugin-данные ограничиваются
по длине и глубине вложенности.

## Особенности корпоративного Confluence

- пространство определяется стабильным `space.key`, а страница — числовым `content.id`;
- типы контента, labels, macros и content properties не предполагаются заранее;
- HTML/XHTML тела преобразуются в текст, исходный markup возвращается только по запросу;
- permissions самой Confluence применяются сервером автоматически;
- при локальном allowlist каждый CQL-запрос дополняется обязательным условием `space in (...)`;
- списки используют штатную пагинацию `start`/`limit` и не загружаются без ограничений;
- вложения по умолчанию только перечисляются: бинарное содержимое не попадает в контекст модели.

## Авторизация

Предпочтительный вариант — отдельный Confluence Personal Access Token:

`https://itpm-wiki.mos.ru/plugins/servlet/personal-access-tokens/manage`

```powershell
$env:DIT_CONFLUENCE_TOKEN = '<PAT>'
```

Токен передаётся как `Authorization: Bearer <PAT>`. Не добавляйте его в YAML,
аргументы командной строки или репозиторий. Jira PAT не переиспользуется автоматически.

Если PAT отключён администратором, предусмотрен Basic Auth:

```powershell
$env:DIT_CONFLUENCE_USERNAME = '<login>'
$env:DIT_CONFLUENCE_PASSWORD = '<password>'
dit-confluence-mcp --auth-type basic --allow-all-visible
```

При корпоративном SSO Basic Auth может быть запрещён. Режим `anonymous` предназначен
только для диагностики общедоступной части API.

## Установка

Из корня DIT Agent:

```powershell
uv pip install --python .\.venv\Scripts\python.exe -e .\services\dit-confluence-mcp
```

## Проверка соединения

```powershell
$env:DIT_CONFLUENCE_TOKEN = '<PAT>'
.\.venv\Scripts\dit-confluence-mcp.exe `
  --base-url https://itpm-wiki.mos.ru `
  --auth-type pat `
  --allow-all-visible `
  --no-env-proxy `
  --doctor
```

`--doctor` показывает версию Confluence, безопасное описание пользователя и видимость
пространств/контента. Секреты не печатаются.

## Подключение к DIT Agent

1. Установить пакет в `.venv`.
2. Добавить блок из `config.example.yaml` в `mcp_servers` изолированного профиля.
3. Добавить `DIT_CONFLUENCE_TOKEN` в `.env` этого профиля.
4. Перезапустить DIT Agent и создать новый чат: набор MCP-инструментов фиксируется в начале
   разговора ради сохранения prompt cache.

## Политика пространств

- `--allow-space KEY` — разрешить конкретное пространство; параметр повторяется.
- `--allow-all-visible` — разрешить всё, что видит Confluence-учётная запись.

При allowlist прямое чтение content ID сначала проверяет пространство, а CQL получает
обязательное ограничение. Сравнение ключей нечувствительно к регистру.

## Сеть

- по умолчанию учитываются `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` и `NO_PROXY`;
- `--no-env-proxy` отключает proxy-переменные только для Confluence;
- `--proxy URL` задаёт отдельный proxy;
- `--ca-bundle PATH` задаёт корпоративный CA bundle.
