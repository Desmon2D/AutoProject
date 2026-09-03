# План реализации Flow Builder — аналога n8n

Статус: реализация, этапы 1–2 завершены, этапы 3–4 выполняются  
Дата: 27 августа 2026 года  
Связанные документы: [архитектура](./architecture.md), [сценарии](./components/scenarios.md), [dashboard](./components/dashboard.md), [оркестратор](./components/orchestrator.md).

Если этот план расходится с `architecture.md`, приоритет имеет `architecture.md`. Документ описывает порядок развития существующего движка, а не отдельную конкурирующую систему.

## 1. Цель

Добавить в dashboard визуальный конструктор процессов, в котором пользователь сможет:

- собирать workflow из типизированных узлов;
- связывать входы и выходы узлов;
- задавать условия, повторные попытки, ограничения и ручные проверки;
- сохранять черновики и публиковать неизменяемые версии;
- запускать workflow вручную или по событию;
- наблюдать выполнение графа и повторять отдельные упавшие узлы;
- использовать опубликованный workflow как один `Subworkflow`-узел другого графа.

Решение не должно копировать n8n целиком. Основная ценность нашей системы — безопасное исполнение агентных и детерминированных шагов, проверка результатов, Gitea/Plane lifecycle и воспроизводимые Docker-песочницы.

## 2. Ключевое архитектурное решение

Flow Builder является новым интерфейсом и расширением текущего workflow engine, а не отдельным сервисом.

Существующие сценарии имеют двойную роль:

1. внутри системы это готовые версионируемые графы;
2. внутри другого workflow они могут вызываться как один `Subworkflow`-узел.

Новая каноническая модель называется `FlowDefinition`. Текущий `ScenarioManifest` сохраняется как совместимый формат и преобразуется в `FlowDefinition` при загрузке.

```text
ScenarioManifest JSON ──adapter──> FlowDefinition ──publish──> FlowVersion
                                      ▲                            │
                                      │                            ▼
Dashboard editor ─────────────── draft API                   WorkflowRun
```

Существующие JSON-файлы сценариев не редактируются dashboard напрямую. Пользователь создаёт на их основе редактируемую копию. Это сохраняет воспроизводимость встроенного каталога и позволяет обновлять поставляемые шаблоны независимо от пользовательских workflow.

## 3. Термины и сущности

| Сущность | Назначение |
|---|---|
| `FlowDefinition` | Текущий редактируемый черновик графа. |
| `FlowVersion` | Опубликованный неизменяемый снимок графа с SHA-256. |
| `FlowNode` | Типизированная операция с входной и выходной схемой. |
| `FlowEdge` | Переход между портами узлов с необязательным условием. |
| `WorkflowRun` | Запуск конкретной опубликованной версии. |
| `NodeRun` | Одна попытка исполнения узла в рамках workflow. |
| `CredentialReference` | Идентификатор секрета без секретного значения в графе. |
| `NodeTypeDefinition` | Описание типа узла для backend, валидации и UI-палитры. |

`FlowDefinition` содержит метаданные, входную схему, граф, trigger-конфигурацию, общие лимиты и настройки доступа. Координаты узлов относятся только к редактору и не влияют на семантику исполнения.

Пример сокращённого формата:

```json
{
  "id": "find-and-repair",
  "revision": 7,
  "title": "Find and repair defects",
  "inputs_schema": {
    "type": "object",
    "required": ["repository", "ref"]
  },
  "nodes": {
    "manual": {
      "type": "trigger.manual",
      "config": {},
      "position": {"x": 80, "y": 180}
    },
    "find": {
      "type": "workflow.subworkflow",
      "config": {"flow_id": "bug-finding", "version": "3"},
      "input_mapping": {
        "repository": "${{ trigger.repository }}",
        "ref": "${{ trigger.ref }}"
      },
      "position": {"x": 360, "y": 180}
    }
  },
  "edges": [
    {"source": "manual.success", "target": "find.input"}
  ]
}
```

## 4. Каталог узлов

### 4.1. MVP

| Категория | Типы узлов |
|---|---|
| Trigger | `manual`, `webhook`, `schedule`, `gitea`, `plane` |
| Execution | `agent`, `command`, `subworkflow` |
| Control | `if`, `switch`, `delay`, `approval`, `finish` |
| Data | `set`, `select`, `merge` |
| Integration | Узлы, предоставленные Gitea, Plane и SWIRL |

### 4.2. После MVP

- ограниченный `loop` и обработка элементов коллекции;
- параллельный `for-each` с лимитом конкуренции;
- batch и rate-limit;
- error boundary для группы узлов;
- reusable group;
- пользовательские connector-узлы из plugin manifest.

Каждый тип узла обязан определить:

- `type` и версию контракта;
- JSON Schema конфигурации;
- схемы входных и выходных портов;
- допустимые бизнес-исходы;
- необходимые plugins, capabilities и credentials;
- стандартные timeout и retry policy;
- наличие внешних side effects;
- способ нормализации и проверки результата;
- UI-метаданные: название, категория, иконка и описание полей.

Backend остаётся источником истины для каталога узлов. Dashboard получает каталог через API и не содержит отдельный захардкоженный список контрактов.

## 5. Модель данных между узлами

Каждый узел получает один JSON-объект и возвращает:

```json
{
  "outcome": "SUCCESS",
  "data": {},
  "artifacts": [],
  "error": null
}
```

Для связывания данных используется безопасный expression language без `eval`:

```text
${{ trigger.repository }}
${{ nodes.find.data.bug_report.findings }}
${{ nodes.tests.outcome }}
```

Рекомендуемая реализация — собственный минимальный template layer над JSONPath или JMESPath. На первом этапе поддерживаются только:

- чтение полей;
- литералы;
- подстановка строк;
- сравнения;
- булевы операции;
- проверка существования и длины.

Произвольный JavaScript или Python в выражениях запрещён. Сложное преобразование выполняется отдельным типизированным `command` или `data`-узлом.

Крупные данные не передаются внутри JSON. Они сохраняются как артефакты, а между узлами передаётся `artifact://`-ссылка.

## 6. Семантика графа

### 6.1. Публикация

Перед публикацией backend обязан проверить:

- уникальность идентификаторов узлов и рёбер;
- наличие ровно одного допустимого входного trigger-пути;
- существование всех портов и совместимость их схем;
- достижимость узлов;
- наличие терминального пути для каждого бизнес-исхода;
- корректность ссылок на credentials, plugins и subworkflow;
- отсутствие неограниченных циклов;
- лимиты timeout, retries, iterations и concurrency;
- невозможность рекурсивного вызова subworkflow;
- права автора на все используемые интеграции.

После проверки создаётся `FlowVersion` с нормализованным JSON и SHA-256. Опубликованная версия не меняется. Новая публикация создаёт следующую версию.

### 6.2. Запуск

1. Trigger создаёт `WorkflowRun` и записывает исходное событие.
2. `event_id` или пользовательский idempotency key защищает от повторной доставки.
3. Run закрепляет точную `FlowVersion` и её hash.
4. Планировщик вычисляет готовые узлы и ставит их в существующую очередь.
5. Worker берёт lease, формирует вход узла и выполняет его.
6. Результат и переход состояния сохраняются атомарно.
7. Планировщик вычисляет активные исходящие рёбра и разблокирует следующие узлы.
8. `approval`, `delay` и внешнее ожидание переводят run в `WAITING`, не удерживая процесс worker.
9. После события продолжения планировщик восстанавливает граф с сохранённого состояния.
10. Run завершается, когда достигнут terminal node и отсутствуют активные ветки.

### 6.3. Готовность узла

- Обычный узел готов после завершения выбранного входного ребра.
- `merge.all` ждёт все активированные входы.
- `merge.any` запускается после первого входа и помечает остальные необязательными.
- Узел, до которого не дошла ни одна активная ветка, получает состояние `SKIPPED`.
- Повтор узла создаёт новый `NodeRun`, не перезаписывая историю попыток.

### 6.4. Ошибки и бизнес-исходы

Технический `ERROR` остаётся отдельным от бизнес-исходов `SUCCESS` и `FAILURE`.

- `ERROR` обрабатывается retry policy или error edge.
- `FAILURE` выбирает бизнес-ветку графа.
- исчерпание retries завершает узел технической ошибкой;
- ручной retry создаёт новую попытку с тем же входным snapshot;
- повтор с изменённым входом оформляется как новый run или явная операция fork.

## 7. Subworkflow

`Subworkflow` вызывает только опубликованную версию другого workflow.

Конфигурация содержит:

- `flow_id`;
- конкретную версию либо политику `latest_published`;
- mapping входов;
- mapping выходов;
- режим ожидания результата;
- timeout и стратегию отмены дочернего run.

Для production-workflow по умолчанию закрепляется конкретная версия. `latest_published` допускается только в черновиках и development-процессах.

Дочерний run получает собственный журнал, но родительский `NodeRun` хранит ссылку на него. Отмена родителя каскадно отменяет дочерний run, если конфигурация не разрешает независимое выполнение.

## 8. Миграция существующих сценариев

### 8.1. Отображение старой модели

| `ScenarioManifest` | `FlowDefinition` |
|---|---|
| `id`, `title`, `description` | Метаданные flow |
| `trigger` | Trigger node |
| `start_step` | Первое ребро trigger |
| `steps` | Набор nodes |
| `transitions.SUCCESS/FAILURE` | Типизированные edges |
| `retry` | Node retry policy |
| `result_contract` | Output schema и validator |
| `timeout_seconds` | Run policy |

### 8.2. Режим совместимости

1. Registry продолжает читать текущие JSON-файлы.
2. Adapter строит из них `FlowDefinition` в памяти.
3. API помечает такие flow как `builtin` и `read_only`.
4. Dashboard отображает их граф и позволяет выполнить `Создать копию`.
5. Workflow engine временно поддерживает старый и новый execution path.
6. После parity-тестов встроенные сценарии переводятся на новый runtime.
7. Старый runtime удаляется только после успешного replay существующих fixtures и E2E-сценариев.

Первым эталонным сценарием для миграции должен стать `bug-finding`: он содержит agent node, command verifier, цикл исправления и два terminal outcome. Затем переносятся `analysis-document`, `implement-ticket` и `test-ticket`.

## 9. Backend-компоненты

Новые логические модули внутри монолитного orchestrator:

| Модуль | Ответственность |
|---|---|
| `flow_models` | Строгие модели draft, version, node, edge и port. |
| `node_catalog` | Регистрация типов узлов и их схем. |
| `flow_validator` | Статическая проверка графа и прав. |
| `flow_compiler` | Нормализация и компиляция published graph. |
| `graph_scheduler` | Вычисление READY, SKIPPED и terminal состояний. |
| `node_runtime` | Общий интерфейс исполнителей узлов. |
| `expression_engine` | Безопасные mapping и условия. |
| `subworkflow_service` | Запуск, ожидание и отмена дочерних run. |
| `flow_repository` | Draft/version persistence и optimistic locking. |

Существующие `workflow_engine`, queue, worker, sandbox manager, artifact store, context builder и plugin registry переиспользуются и постепенно подключаются через `node_runtime`.

## 10. Хранение

Для MVP не добавляется новый контейнер базы данных. Используется отдельная SQLite-база в существующем volume с WAL и миграциями. Интерфейс repository не должен зависеть от SQLite, чтобы позднее добавить PostgreSQL.

Минимальные таблицы:

- `flow_definitions` — identity, owner, revision, draft JSON;
- `flow_versions` — version, normalized JSON, hash, published metadata;
- `workflow_runs` — version, trigger, state, deadlines;
- `node_runs` — node, iteration, attempt, input/output snapshot, lease и status;
- `run_edges` — фактически активированные переходы;
- `credentials` — только metadata и ссылка на secret backend;
- `flow_permissions` — владелец, editor, runner, viewer;
- `audit_events` — публикация, запуск, retry, отмена и операции с credentials.

Запись результата узла, активация рёбер и создание следующих queue jobs выполняются в одной транзакции или через transactional outbox. Это защищает от двойного запуска после падения worker.

## 11. API

Минимальный набор endpoint:

```text
GET    /v1/node-types
GET    /v1/flows
POST   /v1/flows
GET    /v1/flows/{flow_id}
PUT    /v1/flows/{flow_id}/draft
POST   /v1/flows/{flow_id}/validate
POST   /v1/flows/{flow_id}/publish
GET    /v1/flows/{flow_id}/versions
POST   /v1/flows/{flow_id}/runs
GET    /v1/runs/{run_id}
POST   /v1/runs/{run_id}/cancel
POST   /v1/runs/{run_id}/nodes/{node_id}/retry
GET    /v1/runs/{run_id}/events
```

Сохранение draft использует `revision` или ETag. При конфликте dashboard не перезаписывает чужие изменения, а предлагает обновить данные или сохранить копию.

Run events на первом этапе можно отдавать через polling. После стабилизации модели добавляется Server-Sent Events; WebSocket для MVP не требуется.

## 12. UI в dashboard

### 12.1. Маршруты

```text
/flows                         список workflow
/flows/new                     создание
/flows/{flow_id}/edit          визуальный редактор draft
/flows/{flow_id}/versions      опубликованные версии
/runs/{run_id}                 live execution view
```

### 12.2. Экран редактора

```text
┌─────────────────────────────────────────────────────────────────────┐
│ Название | Draft v7 | Validate | Publish | Run                      │
├──────────────┬───────────────────────────────┬──────────────────────┤
│ Палитра      │                               │ Настройки узла       │
│              │          Canvas               │                      │
│ Triggers     │                               │ Inputs / Outputs     │
│ Agent        │                               │ Retry / Timeout      │
│ Control      │                               │ Credentials          │
│ Integrations │                               │                      │
├──────────────┴───────────────────────────────┴──────────────────────┤
│ Ошибки валидации | JSON preview | Test input                        │
└─────────────────────────────────────────────────────────────────────┘
```

Поведение редактора:

- drag-and-drop узлов из палитры;
- соединение только совместимых портов;
- формы конфигурации строятся из схем node catalog;
- autosave с debounce и optimistic locking;
- undo/redo хранится локально до сохранения;
- zoom, pan, minimap и автоматическое выравнивание;
- подсветка недостижимых узлов, циклов и незаполненных обязательных полей;
- просмотр нормализованного JSON без возможности обхода backend-валидации;
- публикация только после успешного `validate`.

Не следует писать собственный canvas engine. Перед UI-этапом проводится короткий spike и выбирается поддерживаемая React-библиотека графов, совместимая с React 19, SSR-ограничениями текущего dashboard, accessibility и лицензией проекта.

### 12.3. Экран выполнения

На том же графе показываются состояния узлов:

- серый — `PENDING` или `SKIPPED`;
- синий — `READY` или `RUNNING`;
- жёлтый — `WAITING`;
- зелёный — `COMPLETED/SUCCESS`;
- оранжевый — `COMPLETED/FAILURE`;
- красный — `ERROR`;
- зачёркнутый — `CANCELLED`.

Выбор узла открывает попытки, входной snapshot, нормализованный результат, логи, артефакты, стоимость модели и причину перехода. Секреты и поля, отмеченные schema как sensitive, всегда скрыты.

## 13. Credentials и безопасность

- Flow хранит только `credential_id`.
- Секрет разрешается непосредственно перед выполнением узла.
- Dashboard никогда не получает сохранённое секретное значение обратно.
- Node type объявляет разрешённые credentials и минимальные scopes.
- Agent node получает только environment allowlist выбранных plugins.
- HTTP node использует allowlist протоколов, DNS/IP-защиту и лимиты ответа.
- Command node выбирает зарегистрированную команду, а не произвольную shell-строку.
- Expression engine не имеет доступа к environment, filesystem или сети.
- Публикация, запуск, retry, approval и изменение credentials записываются в audit log.
- Для side-effect nodes обязательны idempotency key и описанная стратегия повтора.

## 14. Надёжность и ограничения ресурсов

На уровнях flow и node задаются:

- максимальная длительность;
- число попыток и backoff;
- число итераций цикла;
- максимальная параллельность;
- размер входного и выходного JSON;
- число и размер артефактов;
- модельный token/cost budget;
- политика отмены дочерних workflow.

Worker lease, heartbeat и reconcile применяются также к `NodeRun`. После рестарта планировщик должен восстановить потерянные READY/RUNNING узлы без повторения уже подтверждённых side effects.

## 15. Наблюдаемость

Для каждого run сохраняются:

- timeline событий;
- продолжительность ожидания и выполнения каждого узла;
- количество попыток;
- причина выбора каждого ребра;
- provider/model и model usage;
- Docker image digest для agent/command execution;
- ссылки на logs и artifacts;
- parent/child relation для subworkflow;
- request, event и correlation identifiers.

Метрики MVP:

- runs по статусам и типам trigger;
- node duration и error rate;
- queue wait и lease recovery;
- retries;
- число WAITING workflow;
- model calls, tokens и стоимость;
- sandbox start duration.

## 16. Тестовая стратегия

### Unit

- валидация ports, edges, cycles и terminal paths;
- expression parser и evaluator;
- scheduler readiness и branch selection;
- merge semantics;
- retries, timeout и iteration limits;
- scenario-to-flow adapter;
- subworkflow recursion detection.

### Property и state-machine tests

- один завершённый `NodeRun` не исполняется повторно;
- каждый READY node либо получает queue job, либо уже leased;
- активированный edge не исчезает после рестарта;
- terminal run не имеет активных node jobs;
- повторная доставка одного event не создаёт второй run.

### Integration

- SQLite transaction/outbox и queue;
- restart worker во время node execution;
- approval и resume;
- nested subworkflow;
- credentials redaction;
- artifact transfer между узлами.

### UI

- создание и соединение узлов;
- schema-driven forms;
- конфликт revision;
- валидация и публикация;
- отображение live-состояний;
- keyboard navigation и базовая accessibility.

### E2E

1. Собрать копию `bug-finding` через новый формат.
2. Опубликовать её из dashboard.
3. Запустить против `checkout-service-lab`.
4. Получить четыре verified finding.
5. Перезапустить worker в середине выполнения.
6. Убедиться, что run продолжился без дубликатов.
7. Вызвать `bug-finding` как subworkflow родительского процесса.

## 17. Этапы реализации

### Этап 0. ADR и контракты

- [x] Зафиксировать решения по canonical `FlowDefinition`, expression language и storage.
- [x] Описать JSON Schema flow, node, edge и port.
- [x] Определить versioning node types и migration policy.
- [x] Зафиксировать semantics parallel, merge, skip и cancellation.

Принятые решения собраны в [ADR контрактов Flow Builder](./flow-builder-contracts.md).

Критерий готовности: один и тот же пример графа одинаково интерпретируется backend, UI и тестами.

### Этап 1. Read-only граф существующих сценариев

- [x] Реализовать `ScenarioManifest -> FlowDefinition` adapter.
- [x] Добавить `GET /v1/node-types` и read-only flow API.
- [x] Отобразить существующие сценарии в dashboard как граф.
- [x] Добавить просмотр конфигурации узла и переходов.

Критерий готовности: `bug-finding`, `implement-ticket` и `test-ticket` визуализируются без изменения их исполнения.

### Этап 2. Draft и публикация

- [x] Добавить SQLite schema и migrations.
- [x] Реализовать CRUD draft с revision locking.
- [x] Реализовать validator и compiler.
- [x] Реализовать immutable version и hash.
- [x] Добавить clone встроенного сценария в пользовательский draft.

Критерий готовности: пользователь создаёт копию сценария, меняет её и публикует новую независимую версию.

### Этап 3. Graph runtime

- [x] Ввести `NodeRun`, activated edges и `SKIPPED`.
- [x] Подключить существующие agent, command и review executors.
- [ ] Реализовать trigger, if/switch, delay, finish и merge.
  - [x] Trigger и finish через terminal coordination.
  - [x] Бинарные `if` и `switch` (`SUCCESS` = true/match, `FAILURE` = false/default).
  - [x] Persisted `delay`, освобождающий worker и продолжающийся после рестарта.
  - [x] `merge.any` и тривиальный `merge.all` с одним входом.
  - [ ] Multi-port switch, параллельные ветки и `merge.all` с несколькими входами.
- [x] Добавить transaction/outbox и reconcile.
- [x] Реализовать manual retry узла.

Критерий готовности: опубликованный пользовательский граф исполняется после рестарта worker и корректно ветвится.

Единый `NodeRuntime` формирует неизменяемый input snapshot узла: command получает bindings как
типизированные параметры операции, agent — как отдельную секцию контекста, review сохраняет их на
время ожидания решения. Повторная попытка использует тот же snapshot. Initial dispatch и каждое
продолжение графа защищены SQLite outbox и восстанавливаются reconcile. Пользовательский граф
исполняется по одному переходу узла на queue tick. После сохранения результата проекция `NodeRun`,
активация ребра и continuation-outbox фиксируются одной транзакцией GraphRunStore; доставка в очередь
идемпотентна. Legacy-сценарии продолжают использовать прежний execution path.

Control-узлы опубликованы в backend node catalog и dashboard. Пока граф исполняется через линейный
compatibility scheduler, валидатор явно запрещает `merge.all` с несколькими входами: такой граф не
сможет быть опубликован с ложной семантикой. Снятие ограничения требует persisted набора активных
веток и является следующим runtime-инкрементом.

Опубликованные пользовательские flow подписываются на точную пару `trigger.source/event`. Router
использует только последнюю опубликованную enabled-версию, поддерживает fan-out и идемпотентность по
flow/source/event/event_id. События принимаются через `POST /v1/events` и подписанные Plane/Gitea
webhook; push из `refs/heads/automation/*` подавляется для защиты от циклического самозапуска.

### Этап 4. Визуальный редактор

- [ ] Провести spike и выбрать graph UI library.
- [x] Реализовать canvas, palette, property panel и schema forms.
- [x] Вынести редактор на отдельный маршрут `/flows` и добавить drag-and-drop из палитры.
- [x] Добавить drag-and-drop соединений с preview-линей и создание пустого workflow.
- [ ] Добавить autosave.
- [x] Добавить undo/redo и validation panel.
- [x] Добавить publish и manual run.
- [x] Реализовать responsive read-only режим для узких экранов.

Критерий готовности: workflow можно создать и запустить без ручного редактирования JSON.

Промежуточно реализован собственный SVG/CSS canvas без внешней graph library: draft
поддерживает перемещение узлов, добавление drag-and-drop из палитры, drag-and-drop соединений с
временной линией, создание пустого draft, удаление,
локальные undo/redo и schema-driven настройки из
`GET /v1/node-types`, вложенный retry, input bindings и назначение переходов `EVENT`, `SUCCESS`,
`FAILURE`. Trigger event выбирается из зависимого от source каталога. Command получает операции и
формы параметров из `GET /v1/operations`; Agent — модели и плагины из `GET /v1/models` и
`GET /v1/plugins`; Agent и Review используют безопасные ссылки из `GET /v1/credentials`.
Backend validation panel показывает все ошибки и предупреждения; последняя опубликованная версия
запускается вручную с JSON inputs. Autosave остаётся в работе.

### Этап 5. Live execution и Subworkflow

- [ ] Реализовать run graph view и polling/SSE events.
- [ ] Добавить child run lifecycle и pinned versions.
- [ ] Добавить drill-down в subworkflow.
- [ ] Добавить cancel propagation и retry policy.

Критерий готовности: `bug-finding` вызывается как один узел и детально раскрывается в отдельный дочерний run.

### Этап 6. Расширение каталога и hardening

- [ ] Добавить data nodes, bounded loop и parallel for-each.
- [ ] Экспортировать connector nodes из plugin manifests.
- [ ] Добавить RBAC, credential management и budgets.
- [ ] Провести нагрузочные, security и chaos-тесты.
- [ ] Перевести встроенные сценарии на новый runtime.

Критерий готовности: старый execution path не используется и может быть удалён.

## 18. Риски

| Риск | Мера |
|---|---|
| UI и backend по-разному понимают граф | Backend node catalog и validator являются источником истины. |
| Двойные внешние операции после retry | Idempotency key, outbox и persisted side-effect receipt. |
| Неконтролируемые циклы | Только bounded loop или статически ограниченный cycle policy. |
| Сломанные активные runs после публикации | Run закрепляет immutable version и hash. |
| Секрет попадает в graph/output | Credential references, schema redaction и environment allowlist. |
| Слишком сложный MVP | Сначала только текущие node types, if/switch, delay и finish. |
| Canvas становится отдельным источником логики | Positions хранятся отдельно от исполняемой семантики. |
| Subworkflow создаёт рекурсию | Проверка call graph при публикации. |
| SQLite ограничивает параллельность | WAL, короткие транзакции и repository abstraction для PostgreSQL. |

## 19. Что не входит в MVP

- marketplace пользовательского кода;
- выполнение произвольных shell-команд из UI;
- совместное редактирование в реальном времени;
- произвольные скрипты внутри expressions;
- неограниченные циклы;
- автоматическая миграция активного run на новую версию;
- отдельный microservice для редактора или планировщика;
- обязательный PostgreSQL-контейнер в low-memory режиме.

## 20. Definition of Done

Flow Builder можно считать готовым к первому production-пилоту, когда:

- существующие сценарии без потери семантики отображаются как граф;
- пользователь может создать, проверить, опубликовать и запустить workflow из dashboard;
- run закрепляет неизменяемую версию и восстанавливается после рестарта;
- agent, command, review и subworkflow узлы работают через единый runtime;
- retries не дублируют подтверждённые side effects;
- inputs, outputs, transitions, logs и artifacts доступны в run view;
- secrets не возвращаются в UI и не записываются в snapshots;
- `bug-finding` успешно работает и как самостоятельный flow, и как subworkflow;
- backend, UI, migration и E2E-наборы проходят в low-memory профиле.

## 21. Рекомендуемый первый инкремент

Начать не с редактирования, а с read-only визуализации текущих сценариев:

1. добавить adapter `ScenarioManifest -> FlowDefinition`;
2. отдать граф и node catalog через API;
3. показать `bug-finding` на canvas dashboard;
4. связать узлы с уже существующими execution details;
5. только после проверки модели данных добавить сохранение draft.

Такой порядок проверит правильность графовой модели на реальных сценариях и не потребует сразу менять стабильный execution engine.
