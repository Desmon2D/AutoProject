from __future__ import annotations

from collections import defaultdict, deque

from .models import (
    FlowDefinition,
    FlowEdge,
    FlowNode,
    FlowNodeType,
    FlowPosition,
    ScenarioManifest,
)

TRIGGER_NODE_ID = "__trigger__"
SUCCESS_NODE_ID = "__success__"
FAILURE_NODE_ID = "__failure__"
X_GAP = 280
Y_GAP = 140
X_OFFSET = 40
Y_OFFSET = 40
TRIGGER_EVENTS = {
    "manual": [
        "flow.run",
        "analysis.requested",
        "bug-finding.requested",
        "review-demo",
    ],
    "webhook": ["webhook.received"],
    "gitea": [
        "push",
        "pull_request",
        "pull_request_review_approved",
        "pull_request_review_rejected",
        "pull_request_comment",
    ],
    "plane": [
        "issue.ready_for_development",
        "issue.testing",
        "issue.cancelled",
    ],
}


def _retry_schema() -> dict:
    return {
        "type": "object",
        "title": "Повторные попытки",
        "properties": {
            "max_attempts": {
                "type": "integer",
                "title": "Максимум попыток",
                "minimum": 1,
                "maximum": 10,
                "default": 1,
            },
            "delay_seconds": {
                "type": "integer",
                "title": "Начальная задержка, секунд",
                "minimum": 0,
                "maximum": 3600,
                "default": 5,
            },
            "backoff": {
                "type": "string",
                "title": "Интервал между попытками",
                "enum": ["fixed", "exponential"],
                "default": "exponential",
            },
            "max_delay_seconds": {
                "type": "integer",
                "title": "Максимальная задержка, секунд",
                "minimum": 0,
                "maximum": 86_400,
                "default": 300,
            },
        },
    }


def _config_schema(
    properties: dict, required: list[str], *, retry: bool = False
) -> dict:
    fields = dict(properties)
    if retry:
        fields["retry"] = _retry_schema()
    return {"type": "object", "required": required, "properties": fields}


def builtin_node_types() -> list[FlowNodeType]:
    object_schema = {"type": "object"}
    return [
        FlowNodeType(
            type="trigger",
            category="trigger",
            title="Trigger",
            description="Запускает граф по внешнему событию.",
            config_schema=_config_schema(
                {
                    "source": {
                        "type": "string",
                        "title": "Источник",
                        "description": "Источник события для опубликованного flow.",
                        "enum": list(TRIGGER_EVENTS),
                        "minLength": 1,
                    },
                    "event": {
                        "type": "string",
                        "title": "Событие",
                        "description": "Точное имя поддерживаемого события.",
                        "minLength": 1,
                        "x-ui-options-by": {
                            "field": "source",
                            "values": TRIGGER_EVENTS,
                        },
                    },
                },
                ["source", "event"],
            ),
            output_schema=object_schema,
            outcomes=["EVENT"],
        ),
        FlowNodeType(
            type="agent",
            category="execution",
            title="Agent",
            description="Выполняет задачу моделью с подключёнными плагинами.",
            config_schema=_config_schema(
                {
                    "prompt": {
                        "type": "string",
                        "title": "Prompt",
                        "minLength": 1,
                        "maxLength": 20_000,
                        "x-ui-widget": "textarea",
                    },
                    "provider": {
                        "type": "string",
                        "title": "Provider",
                        "enum": ["openai", "openrouter"],
                        "default": "openai",
                    },
                    "model": {
                        "type": "string",
                        "title": "Model",
                        "minLength": 1,
                        "maxLength": 200,
                        "x-ui-catalog": "models",
                    },
                    "credential_id": {
                        "type": "string",
                        "title": "Учётные данные",
                        "description": "Безопасная ссылка на настроенный секрет; сам секрет в flow не сохраняется.",
                        "x-ui-catalog": "credentials",
                        "x-ui-filter-by": "provider",
                    },
                    "plugins": {
                        "type": "array",
                        "title": "Плагины",
                        "description": "По одному идентификатору плагина на строку.",
                        "items": {"type": "string"},
                        "maxItems": 32,
                        "default": [],
                        "x-ui-catalog": "plugins",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "title": "Timeout, секунд",
                        "minimum": 1,
                        "maximum": 3600,
                        "default": 600,
                    },
                    "result_contract": {
                        "type": "string",
                        "title": "Контракт результата",
                        "enum": [
                            "none",
                            "pull_request",
                            "implementation_change",
                            "test_change",
                            "test_execution",
                            "markdown_document",
                            "bug_report",
                        ],
                        "default": "none",
                    },
                },
                ["prompt", "provider", "model"],
                retry=True,
            ),
            input_schema=object_schema,
            output_schema=object_schema,
            outcomes=["SUCCESS", "FAILURE"],
        ),
        FlowNodeType(
            type="command",
            category="execution",
            title="Command",
            description="Выполняет зарегистрированную серверную команду.",
            config_schema=_config_schema(
                {
                    "command": {
                        "type": "string",
                        "title": "Операция",
                        "description": "Идентификатор зарегистрированной серверной операции.",
                        "minLength": 1,
                        "x-ui-catalog": "operations",
                    },
                    "parameters": {
                        "type": "object",
                        "title": "Статические параметры",
                        "description": "JSON-объект параметров операции.",
                        "default": {},
                        "x-ui-schema-from": {
                            "catalog": "operations",
                            "selector": "command",
                            "field": "input_schema",
                        },
                    },
                },
                ["command", "parameters"],
                retry=True,
            ),
            input_schema=object_schema,
            output_schema=object_schema,
            outcomes=["SUCCESS", "FAILURE"],
        ),
        FlowNodeType(
            type="review",
            category="control",
            title="Review",
            description="Останавливает граф до решения пользователя или внешней системы.",
            config_schema=_config_schema(
                {
                    "provider": {
                        "type": "string",
                        "title": "Provider",
                        "enum": ["gitea", "plane"],
                        "default": "gitea",
                    },
                    "decision": {
                        "type": "string",
                        "title": "Ожидаемое решение",
                        "enum": ["review", "merge"],
                        "default": "review",
                    },
                    "credential_id": {
                        "type": "string",
                        "title": "Учётные данные",
                        "description": "Безопасная ссылка на настроенный секрет; сам секрет в flow не сохраняется.",
                        "x-ui-catalog": "credentials",
                        "x-ui-filter-by": "provider",
                    },
                },
                ["provider", "decision"],
                retry=True,
            ),
            input_schema=object_schema,
            output_schema=object_schema,
            outcomes=["SUCCESS", "FAILURE"],
        ),
        FlowNodeType(
            type="if",
            category="control",
            title="If",
            description="Выбирает SUCCESS или FAILURE по безопасному boolean-выражению.",
            config_schema=_config_schema(
                {
                    "condition": {
                        "type": "string",
                        "title": "Условие",
                        "description": "Безопасное выражение в формате ${{ expression }}.",
                        "minLength": 1,
                    }
                },
                ["condition"],
                retry=True,
            ),
            input_schema=object_schema,
            output_schema=object_schema,
            outcomes=["SUCCESS", "FAILURE"],
        ),
        FlowNodeType(
            type="switch",
            category="control",
            title="Switch",
            description="Сравнивает значение: SUCCESS означает match, FAILURE — default.",
            config_schema=_config_schema(
                {
                    "value": {
                        "type": "string",
                        "title": "Значение",
                        "description": "Безопасное выражение в формате ${{ expression }}.",
                        "minLength": 1,
                    },
                    "equals": {
                        "type": ["string", "number", "boolean", "null"],
                        "title": "Равно",
                        "description": "JSON-скаляр: строка, число, true, false или null.",
                    },
                },
                ["value", "equals"],
                retry=True,
            ),
            input_schema=object_schema,
            output_schema=object_schema,
            outcomes=["SUCCESS", "FAILURE"],
        ),
        FlowNodeType(
            type="delay",
            category="control",
            title="Delay",
            description="Приостанавливает run без удержания worker.",
            config_schema=_config_schema(
                {
                    "seconds": {
                        "type": "integer",
                        "title": "Задержка, секунд",
                        "minimum": 0,
                        "maximum": 86_400,
                    }
                },
                ["seconds"],
                retry=True,
            ),
            input_schema=object_schema,
            output_schema=object_schema,
            outcomes=["SUCCESS", "FAILURE"],
        ),
        FlowNodeType(
            type="merge",
            category="data",
            title="Merge",
            description="Продолжает активированный путь; parallel merge.all пока ограничен.",
            config_schema=_config_schema(
                {
                    "mode": {
                        "type": "string",
                        "title": "Режим",
                        "enum": ["any", "all"],
                        "default": "any",
                    }
                },
                ["mode"],
                retry=True,
            ),
            input_schema=object_schema,
            output_schema=object_schema,
            outcomes=["SUCCESS", "FAILURE"],
        ),
        FlowNodeType(
            type="terminal",
            category="terminal",
            title="Terminal",
            description="Завершает граф с успешным или неуспешным исходом.",
            config_schema=_config_schema(
                {
                    "outcome": {
                        "type": "string",
                        "title": "Outcome",
                        "enum": ["SUCCESS", "FAILURE"],
                        "default": "SUCCESS",
                    }
                },
                ["outcome"],
            ),
            input_schema=object_schema,
        ),
    ]


def scenario_to_flow(scenario: ScenarioManifest) -> FlowDefinition:
    levels = _step_levels(scenario)
    terminal_outcomes = {
        outcome
        for step in scenario.steps.values()
        for outcome, target in step.transitions.items()
        if target is None
    }
    terminal_level = max(levels.values(), default=0) + 1
    level_members: dict[int, list[str]] = defaultdict(list)
    level_members[0].append(TRIGGER_NODE_ID)
    for step_id in scenario.steps:
        level_members[levels[step_id]].append(step_id)
    if "SUCCESS" in terminal_outcomes:
        level_members[terminal_level].append(SUCCESS_NODE_ID)
    if "FAILURE" in terminal_outcomes:
        level_members[terminal_level].append(FAILURE_NODE_ID)

    positions = {
        node_id: FlowPosition(x=X_OFFSET + level * X_GAP, y=Y_OFFSET + index * Y_GAP)
        for level, node_ids in level_members.items()
        for index, node_id in enumerate(node_ids)
    }
    nodes = [
        FlowNode(
            id=TRIGGER_NODE_ID,
            type="trigger",
            category="trigger",
            title="Trigger",
            subtitle=f"{scenario.trigger.source} · {scenario.trigger.event}",
            config=scenario.trigger.model_dump(),
            position=positions[TRIGGER_NODE_ID],
        )
    ]
    for step_id, step in scenario.steps.items():
        category = "execution"
        if step.type in {"review", "if", "switch", "delay"}:
            category = "control"
        elif step.type == "merge":
            category = "data"
        nodes.append(
            FlowNode(
                id=step_id,
                type=step.type,
                category=category,
                title=step_id,
                subtitle=_step_subtitle(step),
                config=step.model_dump(exclude={"type", "transitions", "input_mapping"}),
                input_mapping=step.input_mapping,
                position=positions[step_id],
            )
        )
    if "SUCCESS" in terminal_outcomes:
        nodes.append(_terminal_node(SUCCESS_NODE_ID, "Success", "SUCCESS", positions))
    if "FAILURE" in terminal_outcomes:
        nodes.append(_terminal_node(FAILURE_NODE_ID, "Failure", "FAILURE", positions))

    edges = [
        FlowEdge(
            id=f"{TRIGGER_NODE_ID}:EVENT:{scenario.start_step}",
            source=TRIGGER_NODE_ID,
            source_port="EVENT",
            target=scenario.start_step,
            label="EVENT",
            kind="event",
        )
    ]
    for step_id, step in scenario.steps.items():
        for outcome in ("SUCCESS", "FAILURE"):
            target = step.transitions[outcome]
            if target is None:
                target = SUCCESS_NODE_ID if outcome == "SUCCESS" else FAILURE_NODE_ID
            edges.append(
                FlowEdge(
                    id=f"{step_id}:{outcome}:{target}",
                    source=step_id,
                    source_port=outcome,
                    target=target,
                    label=outcome,
                    kind="transition",
                    outcome=outcome,
                )
            )

    return FlowDefinition(
        id=scenario.id,
        version=scenario.version,
        title=scenario.title or scenario.id,
        description=scenario.description,
        stage=scenario.stage,
        enabled=scenario.enabled,
        source_scenario_id=scenario.id,
        start_node=scenario.start_step,
        nodes=nodes,
        edges=edges,
    )


def _step_levels(scenario: ScenarioManifest) -> dict[str, int]:
    levels = {scenario.start_step: 1}
    queue = deque([scenario.start_step])
    while queue:
        step_id = queue.popleft()
        for target in scenario.steps[step_id].transitions.values():
            if target is not None and target not in levels:
                levels[target] = levels[step_id] + 1
                queue.append(target)
    fallback_level = max(levels.values(), default=0) + 1
    for step_id in scenario.steps:
        if step_id not in levels:
            levels[step_id] = fallback_level
            fallback_level += 1
    return levels


def _step_subtitle(step) -> str:
    if step.type == "agent":
        return f"{step.provider} · {step.model}"
    if step.type == "command":
        return step.command
    if step.type == "if":
        return step.condition
    if step.type == "switch":
        return f"match · {step.equals!r}"
    if step.type == "delay":
        return f"{step.seconds}s"
    if step.type == "merge":
        return step.mode
    return f"{step.provider} · {step.decision}"


def _terminal_node(node_id, title, outcome, positions) -> FlowNode:
    return FlowNode(
        id=node_id,
        type="terminal",
        category="terminal",
        title=title,
        subtitle="Сценарий завершён",
        config={"outcome": outcome},
        position=positions[node_id],
    )
