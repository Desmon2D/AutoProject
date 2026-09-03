# DIT Git MCP

Локальный MCP-сервер с доступом на чтение к `https://git.mos.ru` через GitLab REST API v4.

## Read-only возможности

- поиск и получение метаданных разрешённых проектов;
- ограниченное дерево репозитория глубиной до пяти уровней;
- чтение диапазона строк или пакета до десяти UTF-8/UTF-16 файлов;
- поиск по коду внутри выбранного проекта;
- просмотр веток, тегов и истории коммитов;
- получение коммита с ограниченным diff и сравнение двух refs;
- просмотр merge requests, их commits, diffs, discussions и approvals;
- просмотр pipelines, jobs и ограниченного хвоста job log;
- blame для ограниченного диапазона строк.

Все ответы ограничиваются по количеству объектов и объёму текста. Из файлов,
обсуждений и CI-логов удаляются распространённые форматы секретов. Пути к
`.env`, приватным ключам и контейнерам сертификатов блокируются до обращения к
GitLab.

Сервер не содержит инструментов clone, push, merge, изменения файлов или merge request.

## Требования безопасности

По умолчанию сервер не запускается без `--allow-group` или `--allow-project`. Результаты дополнительно фильтруются по allowlist, даже если токен видит больше проектов. Явный параметр `--allow-all-visible` снимает фильтр namespace и разрешает чтение всех проектов, доступных токену.

Токен передаётся только через `DIT_GIT_TOKEN`. Не добавляйте его в YAML, аргументы командной строки или репозиторий.

Для начала используйте отдельный GitLab PAT с минимальными правами на чтение и доступом только к тестовой группе. Если у `git.mos.ru` собственный центр сертификации, передайте PEM-файл через `--ca-bundle`.

## Установка для разработки

Из корня DIT Agent:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .\services\dit-git-mcp
```

## Проверка соединения

```powershell
$env:DIT_GIT_TOKEN = '<PAT>'
.\.venv\Scripts\dit-git-mcp.exe `
  --base-url https://git.mos.ru `
  --allow-group '<GROUP>' `
  --doctor
```

Проверка выводит только URL, имя и username авторизованного пользователя. Токен не печатается.

## Подключение к DIT Agent

1. Скопируйте `config.example.yaml` в секцию `mcp_servers` активного профиля.
2. Замените `REPLACE_WITH_ALLOWED_GROUP`.
3. Сохраните PAT в секретном окружении профиля как `DIT_GIT_TOKEN`.
4. Перезапустите новую сессию DIT Agent, чтобы её набор MCP-инструментов был стабилен с первого сообщения.

## Параметры

```text
--base-url URL             GitLab instance, по умолчанию https://git.mos.ru
--auth-type TYPE           private-token (PAT) или bearer (OAuth)
--allow-group GROUP        Разрешить группу и её подгруппы; можно повторять
--allow-project PROJECT    Разрешить отдельный group/project; можно повторять
--allow-all-visible        Разрешить все проекты, доступные токену
--ca-bundle PATH           Корпоративный CA bundle в PEM
--proxy URL                Корпоративный HTTPS proxy
--timeout SECONDS          Таймаут REST-запроса
--retries COUNT            Повторы 429/502/503/504 и сетевых сбоев
--max-file-bytes BYTES     Максимальный размер читаемого файла
--max-tree-entries COUNT   Максимум элементов дерева в одном ответе
--max-diff-chars COUNT     Общий лимит символов diff/пакета файлов
--max-job-log-chars COUNT  Максимальный доступный хвост CI job log
```
