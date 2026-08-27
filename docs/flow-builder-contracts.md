# ADR: контракты Flow Builder

Статус: принято 27 августа 2026 года.

Этот документ фиксирует решения этапа 0 плана Flow Builder. Исполняемый код и OpenAPI остаются
источником истины; изменение перечисленных контрактов требует новой версии соответствующего типа.

## Каноническая модель

`FlowDefinition` является единственным редактируемым представлением графа. Встроенный
`ScenarioManifest` преобразуется адаптером в read-only `FlowDefinition`, а пользовательский draft
публикуется как неизменяемый `FlowVersion` с нормализованным JSON и SHA-256.

JSON Schema моделей `FlowDefinition`, `FlowNode`, `FlowEdge` и `FlowNodeType` генерируется Pydantic
и публикуется в OpenAPI. Дублирующий вручную поддерживаемый schema-файл не создаётся.

- `FlowNode.type` определяет runtime и набор портов.
- `FlowNode.config` проверяется backend-контрактом типа узла.
- `FlowNode.position` относится только к UI.
- `FlowEdge.source_port` обязан существовать в `FlowNodeType.outcomes`.
- `FlowNodeType.input_schema` и `output_schema` описывают JSON на границах узла.
- Command-узел выбирает зарегистрированный `OperationDefinition`; произвольная shell-команда
  недопустима.

## Версионирование

- `FlowDefinition.revision` используется только для optimistic locking draft.
- `FlowVersion.version` монотонно увеличивается; опубликованная версия неизменяема.
- `FlowNodeType.version` меняется при несовместимом изменении config или портов.
- `OperationDefinition.version` меняется при несовместимом изменении входа, выхода или outcomes.
- Активный run всегда закрепляет точные flow version и SHA-256.
- Миграция изменяет только draft или создаёт новую опубликованную версию; активные run не
  мигрируют.

## Операции

`GET /v1/operations` возвращает стабильный идентификатор, версию, категорию, JSON Schema входа и
выхода, outcomes, ошибки, integrations, capabilities, side-effect metadata и executor key.
Операция с внешним side effect обязана иметь idempotency key на уровне run/node/operation.

Trigger source и event выбираются из backend-каталога как совместимая пара. Dashboard не предлагает
свободный ввод события, а backend отклоняет пару, отсутствующую в каталоге. Модели Agent, плагины и
ссылки на credentials также загружаются через API; секретное значение ни один каталог не возвращает.

На переходном этапе идентификаторы операций совпадают с legacy `command`. Каталог уже является
источником allowlist и валидации, но выполнение остаётся в compatibility executor до завершения
миграции.

## Expression language и data bindings

Синтаксис выражения: `${{ <expression> }}`. Разрешены только:

- корни `inputs`, `trigger` и `nodes.<node_id>`;
- чтение JSON-полей по точечной нотации;
- литералы string, number, boolean и null;
- `==`, `!=`, `<`, `<=`, `>`, `>=`;
- `and`, `or`, `not`;
- функции `exists(value)` и `length(value)`.

Нет `eval`, вызова пользовательских функций, доступа к environment, filesystem или сети.
Bindings валидируются при публикации: ссылка только на достижимый предыдущий узел, а output schema
источника должна быть совместима с input schema назначения. Крупные данные передаются только как
`artifact://` references.

`NodeRuntime` вычисляет bindings до запуска и сохраняет их в input snapshot `NodeRun`. Вложенные
пути (`data.ticket.id`) собираются в JSON и дополняют статическую конфигурацию. Command использует
snapshot как параметры зарегистрированной операции, agent получает его отдельной секцией
`node_inputs`, review сохраняет snapshot до внешнего решения. Retry использует сохранённое значение.

## Семантика исполнения

- Узел становится `READY`, когда выполнено активированное входное условие.
- Обычный узел запускается после одного выбранного входа.
- `merge.all` ждёт все активированные входы; `merge.any` — первый вход.
- Узел без активированного пути получает `SKIPPED`.
- Параллельные ветки независимы до merge или terminal coordination.
- `ERROR` является техническим состоянием и обрабатывается retry/error edge.
- `FAILURE` является бизнес-исходом и выбирает соответствующий порт.
- Ручной retry создаёт новый `NodeRun` с тем же input snapshot.
- Отмена запрещает новые dispatch, отменяет ожидающие jobs и каскадно отменяет дочерние run,
  если subworkflow явно не объявлен независимым.
- После durable-записи результата проекция `NodeRun`, activation edges и continuation-outbox
  выполняются одной транзакцией GraphRunStore. Доставка outbox в отдельную очередь идемпотентна;
  reconcile восстанавливает продолжение после сбоя на границе хранилищ.

Пользовательский граф исполняет не более одного перехода узла за queue tick. Это даёт scheduler
устойчивую точку восстановления между узлами; legacy-сценарии сохраняют прежний execution path.

Trigger router сопоставляет точные `source` и `event` с последней опубликованной enabled-версией
каждого flow. Одно событие может запустить несколько flow. Run ID детерминирован по
flow/source/event/event_id, поэтому повторная доставка возвращает исходный run и не создаёт новый.
Исходный `TriggerEvent` сохраняется вместе с run и используется в expression context.

Текущий бинарный control MVP использует общие порты: для `if` это true/false, для `switch` —
match/default. `delay` сохраняет `PendingDelay`, возвращает lease worker и возобновляется очередью в
`available_at`. `merge.any` исполняется после активного входа. `merge.all` с несколькими входами
отклоняется кодом `merge-all-parallel-unavailable`, пока scheduler не хранит несколько активных
веток; нормативная семантика выше остаётся целевой.

## Хранение

MVP использует отдельные SQLite repositories в общем persistent volume, WAL, короткие транзакции и
optimistic locking. Repository interfaces не зависят от SQLite, чтобы позднее заменить хранилище
на PostgreSQL без изменения runtime contracts. Секреты в flow JSON не хранятся — только
`credential_id`.
