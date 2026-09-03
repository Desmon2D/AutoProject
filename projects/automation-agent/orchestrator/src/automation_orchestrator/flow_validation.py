from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict, deque

from pydantic import ValidationError

from .expression_engine import ExpressionError, template_references
from .flow_builder import TRIGGER_EVENTS
from .models import (
    COMMAND_PATTERN,
    PLUGIN_PATTERN,
    FlowDefinition,
    FlowValidationIssue,
    FlowValidationResult,
    RetryPolicy,
)
from .operation_catalog import get_operation, validate_operation_parameters
from .runtime_catalog import CREDENTIAL_PROVIDERS

RESERVED_NODE_IDS = {"__trigger__", "__success__", "__failure__"}
BINDING_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
EXECUTABLE_NODE_TYPES = {
    "agent",
    "command",
    "review",
    "if",
    "switch",
    "delay",
    "merge",
}


def validate_flow(flow: FlowDefinition) -> FlowValidationResult:
    errors: list[FlowValidationIssue] = []
    warnings: list[FlowValidationIssue] = []
    node_ids = [node.id for node in flow.nodes]
    known_nodes = set(node_ids)

    if len(flow.nodes) > 250:
        errors.append(
            FlowValidationIssue(code="node-limit", message="Граф содержит больше 250 узлов.")
        )
    if len(flow.edges) > 1000:
        errors.append(
            FlowValidationIssue(
                code="edge-limit", message="Граф содержит больше 1000 переходов."
            )
        )

    _duplicates(node_ids, "duplicate-node", "Повторяющийся идентификатор узла", errors)
    _duplicates(
        [edge.id for edge in flow.edges],
        "duplicate-edge",
        "Повторяющийся идентификатор перехода",
        errors,
        edge=True,
    )
    for node in flow.nodes:
        if node.id not in RESERVED_NODE_IDS and not PLUGIN_PATTERN.fullmatch(node.id):
            errors.append(
                FlowValidationIssue(
                    code="invalid-node-id",
                    message="Идентификатор узла должен быть в kebab-case.",
                    node_id=node.id,
                )
            )
        if not node.title.strip():
            errors.append(
                FlowValidationIssue(
                    code="node-title-empty",
                    message="Название узла не может быть пустым.",
                    node_id=node.id,
                )
            )
        _validate_node_config(node, errors)

    triggers = [node for node in flow.nodes if node.type == "trigger"]
    terminals = [node for node in flow.nodes if node.type == "terminal"]
    if len(triggers) != 1:
        errors.append(
            FlowValidationIssue(
                code="trigger-count",
                message="Граф должен содержать ровно один trigger-узел.",
            )
        )
    if not terminals:
        errors.append(
            FlowValidationIssue(
                code="terminal-missing",
                message="Граф должен содержать хотя бы один terminal-узел.",
            )
        )
    if flow.start_node not in known_nodes:
        errors.append(
            FlowValidationIssue(
                code="start-node-missing",
                message="Стартовый узел отсутствует в графе.",
                node_id=flow.start_node,
            )
        )
    else:
        start = next(node for node in flow.nodes if node.id == flow.start_node)
        if start.type in {"trigger", "terminal"}:
            errors.append(
                FlowValidationIssue(
                    code="invalid-start-node",
                    message="Стартовым должен быть исполняемый или control-узел.",
                    node_id=start.id,
                )
            )

    outgoing: dict[str, list] = defaultdict(list)
    incoming: dict[str, list] = defaultdict(list)
    adjacency: dict[str, list[str]] = defaultdict(list)
    nodes_by_id = {node.id: node for node in flow.nodes}
    for edge in flow.edges:
        if edge.source not in known_nodes:
            errors.append(
                FlowValidationIssue(
                    code="edge-source-missing",
                    message="Источник перехода отсутствует в графе.",
                    edge_id=edge.id,
                )
            )
        if edge.target not in known_nodes:
            errors.append(
                FlowValidationIssue(
                    code="edge-target-missing",
                    message="Цель перехода отсутствует в графе.",
                    edge_id=edge.id,
                )
            )
        if edge.source in known_nodes and edge.target in known_nodes:
            outgoing[edge.source].append(edge)
            incoming[edge.target].append(edge)
            adjacency[edge.source].append(edge.target)

    for terminal in terminals:
        if outgoing[terminal.id]:
            errors.append(
                FlowValidationIssue(
                    code="terminal-has-output",
                    message="Terminal-узел не может иметь исходящих переходов.",
                    node_id=terminal.id,
                )
            )
    for node in flow.nodes:
        _validate_input_mapping(node, known_nodes, adjacency, errors)
    for node in flow.nodes:
        node_edges = outgoing[node.id]
        ports = [edge.source_port for edge in node_edges]
        duplicate_ports = {port for port in ports if ports.count(port) > 1}
        for port in sorted(duplicate_ports):
            errors.append(
                FlowValidationIssue(
                    code="duplicate-output-port",
                    message=f"Порт {port} имеет больше одного перехода.",
                    node_id=node.id,
                )
            )
        if node.type in EXECUTABLE_NODE_TYPES:
            missing = {"SUCCESS", "FAILURE"} - set(ports)
            if missing:
                errors.append(
                    FlowValidationIssue(
                        code="transition-missing",
                        message=f"Не определены переходы: {', '.join(sorted(missing))}.",
                        node_id=node.id,
                    )
                )
            for edge in node_edges:
                if (
                    edge.source_port not in {"SUCCESS", "FAILURE"}
                    or edge.kind != "transition"
                    or edge.outcome != edge.source_port
                ):
                    errors.append(
                        FlowValidationIssue(
                            code="invalid-transition-port",
                            message="Исполняемый узел поддерживает только SUCCESS/FAILURE.",
                            edge_id=edge.id,
                        )
                    )
                target = nodes_by_id.get(edge.target)
                if (
                    target is not None
                    and target.type == "terminal"
                    and edge.outcome in {"SUCCESS", "FAILURE"}
                    and target.config.get("outcome") != edge.outcome
                ):
                    errors.append(
                        FlowValidationIssue(
                            code="terminal-outcome-mismatch",
                            message="Transition outcome does not match terminal outcome.",
                            edge_id=edge.id,
                        )
                    )

        if (
            node.type == "merge"
            and node.config.get("mode") == "all"
            and len(incoming[node.id]) > 1
        ):
            errors.append(
                FlowValidationIssue(
                    code="merge-all-parallel-unavailable",
                    message="merge.all with multiple inputs requires the parallel scheduler.",
                    node_id=node.id,
                )
            )
    if len(triggers) == 1:
        trigger_edges = outgoing[triggers[0].id]
        if len(trigger_edges) != 1 or trigger_edges[0].target != flow.start_node:
            errors.append(
                FlowValidationIssue(
                    code="trigger-start-mismatch",
                    message="Trigger должен иметь один переход к стартовому узлу.",
                    node_id=triggers[0].id,
                )
            )
        elif (
            trigger_edges[0].source_port != "EVENT"
            or trigger_edges[0].kind != "event"
            or trigger_edges[0].outcome is not None
        ):
            errors.append(
                FlowValidationIssue(
                    code="invalid-trigger-port",
                    message="Trigger-переход должен использовать порт EVENT.",
                    edge_id=trigger_edges[0].id,
                )
            )
        reachable = _reachable(triggers[0].id, adjacency)
        for node_id in sorted(known_nodes - reachable):
            warnings.append(
                FlowValidationIssue(
                    code="unreachable-node",
                    message="Узел недостижим из trigger.",
                    node_id=node_id,
                )
            )

    valid = not errors
    return FlowValidationResult(
        valid=valid,
        errors=errors,
        warnings=warnings,
        sha256=flow_digest(flow) if valid else None,
    )


def _validate_node_config(node, errors: list[FlowValidationIssue]) -> None:
    config = node.config

    def required_string(key: str, code: str) -> str | None:
        value = config.get(key)
        if not isinstance(value, str) or not value.strip():
            errors.append(
                FlowValidationIssue(
                    code=code,
                    message=f"Поле config.{key} обязательно.",
                    node_id=node.id,
                )
            )
            return None
        return value

    if node.type in EXECUTABLE_NODE_TYPES:
        try:
            RetryPolicy.model_validate(config.get("retry", {}))
        except ValidationError as exc:
            errors.append(
                FlowValidationIssue(
                    code="retry-policy-invalid",
                    message=f"Некорректная retry policy: {exc.errors(include_url=False)[0]['msg']}.",
                    node_id=node.id,
                )
            )

    if node.type == "trigger":
        source = required_string("source", "trigger-source-missing")
        required_string("event", "trigger-event-missing")
        if source is not None and source not in TRIGGER_EVENTS:
            errors.append(
                FlowValidationIssue(
                    code="trigger-source-invalid",
                    message="Trigger source должен быть manual, webhook, gitea или plane.",
                    node_id=node.id,
                )
            )
        elif source is not None and config.get("event") not in TRIGGER_EVENTS[source]:
            errors.append(
                FlowValidationIssue(
                    code="trigger-event-invalid",
                    message=f"Событие не поддерживается для trigger source {source}.",
                    node_id=node.id,
                )
            )
    elif node.type == "agent":
        required_string("prompt", "agent-prompt-missing")
        provider = required_string("provider", "agent-provider-missing")
        required_string("model", "agent-model-missing")
        if provider is not None and provider not in {"openai", "openrouter"}:
            errors.append(
                FlowValidationIssue(
                    code="agent-provider-invalid",
                    message="Agent provider должен быть openai или openrouter.",
                    node_id=node.id,
                )
            )
        _validate_credential_reference(node, provider, errors)
        plugins = config.get("plugins", [])
        plugins_are_names = isinstance(plugins, list) and all(
            isinstance(plugin, str) and PLUGIN_PATTERN.fullmatch(plugin)
            for plugin in plugins
        )
        if (
            not plugins_are_names
            or len(plugins) > 32
            or len(set(plugins)) != len(plugins)
        ):
            errors.append(
                FlowValidationIssue(
                    code="agent-plugins-invalid",
                    message="Agent plugins должны быть уникальным списком kebab-case идентификаторов.",
                    node_id=node.id,
                )
            )
        timeout_seconds = config.get("timeout_seconds", 600)
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600:
            errors.append(
                FlowValidationIssue(
                    code="agent-timeout-invalid",
                    message="config.timeout_seconds must be an integer from 1 to 3600.",
                    node_id=node.id,
                )
            )
        if config.get("result_contract", "none") not in {
            "none",
            "pull_request",
            "implementation_change",
            "test_change",
            "test_execution",
            "markdown_document",
            "bug_report",
        }:
            errors.append(
                FlowValidationIssue(
                    code="agent-result-contract-invalid",
                    message="Неизвестный контракт результата Agent.",
                    node_id=node.id,
                )
            )
    elif node.type == "command":
        command = required_string("command", "command-name-missing")
        if command is not None and not COMMAND_PATTERN.fullmatch(command):
            errors.append(
                FlowValidationIssue(
                    code="command-name-invalid",
                    message="Недопустимое имя зарегистрированной команды.",
                    node_id=node.id,
                )
            )
        elif command is not None and get_operation(command) is None:
            errors.append(
                FlowValidationIssue(
                    code="command-not-allowlisted",
                    message="Операция отсутствует в каталоге оркестратора.",
                    node_id=node.id,
                )
            )
        elif command is not None:
            parameters = config.get("parameters", {})
            bound_parameters = {path.split(".", 1)[0] for path in node.input_mapping}
            operation = get_operation(command)
            properties = (
                operation.input_schema.get("properties", {}) if operation is not None else {}
            )
            for path in node.input_mapping:
                root, separator, _ = path.partition(".")
                if root not in properties:
                    errors.append(
                        FlowValidationIssue(
                            code="binding-parameter-unknown",
                            message=f"Operation has no input parameter: {root}.",
                            node_id=node.id,
                        )
                    )
                elif separator and properties[root].get("type") != "object":
                    errors.append(
                        FlowValidationIssue(
                            code="binding-parameter-path-invalid",
                            message=f"Operation parameter is not an object: {root}.",
                            node_id=node.id,
                        )
                    )
            for message in validate_operation_parameters(
                command,
                parameters,
                bound_parameters=bound_parameters,
            ):
                errors.append(
                    FlowValidationIssue(
                        code="command-parameters-invalid",
                        message=message,
                        node_id=node.id,
                    )
                )
    elif node.type == "review":
        provider = required_string("provider", "review-provider-missing")
        decision = required_string("decision", "review-decision-missing")
        if provider is not None and provider not in {"gitea", "plane"}:
            errors.append(
                FlowValidationIssue(
                    code="review-provider-invalid",
                    message="Review provider должен быть gitea или plane.",
                    node_id=node.id,
                )
            )
        _validate_credential_reference(node, provider, errors)
        if decision is not None and decision not in {"review", "merge"}:
            errors.append(
                FlowValidationIssue(
                    code="review-decision-invalid",
                    message="Review decision должен быть review или merge.",
                    node_id=node.id,
                )
            )
    elif node.type == "if":
        condition = required_string("condition", "if-condition-missing")
        if condition is not None:
            _validate_control_expression(node, "condition", condition, errors)
    elif node.type == "switch":
        value = required_string("value", "switch-value-missing")
        if value is not None:
            _validate_control_expression(node, "value", value, errors)
        if "equals" not in config or not isinstance(
            config.get("equals"), (str, int, float, bool, type(None))
        ):
            errors.append(
                FlowValidationIssue(
                    code="switch-equals-invalid",
                    message="config.equals must be a JSON scalar.",
                    node_id=node.id,
                )
            )
    elif node.type == "delay":
        seconds = config.get("seconds")
        if type(seconds) is not int or not 0 <= seconds <= 86_400:
            errors.append(
                FlowValidationIssue(
                    code="delay-seconds-invalid",
                    message="config.seconds must be an integer from 0 to 86400.",
                    node_id=node.id,
                )
            )
    elif node.type == "merge" and config.get("mode") not in {"any", "all"}:
        errors.append(
            FlowValidationIssue(
                code="merge-mode-invalid",
                message="config.mode must be any or all.",
                node_id=node.id,
            )
        )
    elif node.type == "terminal" and config.get("outcome") not in {"SUCCESS", "FAILURE"}:
        errors.append(
            FlowValidationIssue(
                code="terminal-outcome-invalid",
                message="Terminal outcome должен быть SUCCESS или FAILURE.",
                node_id=node.id,
            )
        )


def _validate_credential_reference(node, provider, errors) -> None:
    credential_id = node.config.get("credential_id")
    if credential_id is None:
        return
    expected_provider = CREDENTIAL_PROVIDERS.get(credential_id)
    if expected_provider is None:
        errors.append(
            FlowValidationIssue(
                code="credential-reference-invalid",
                message="Неизвестная ссылка на учётные данные.",
                node_id=node.id,
            )
        )
    elif provider is not None and expected_provider != provider:
        errors.append(
            FlowValidationIssue(
                code="credential-provider-mismatch",
                message="Учётные данные не соответствуют provider узла.",
                node_id=node.id,
            )
        )


def _validate_control_expression(node, field: str, template: str, errors) -> None:
    if not (template.strip().startswith("${{") and template.strip().endswith("}}")):
        errors.append(
            FlowValidationIssue(
                code="control-expression-invalid",
                message=f"config.{field} must use ${{{{ expression }}}} syntax.",
                node_id=node.id,
            )
        )
        return
    try:
        template_references(template)
    except ExpressionError as exc:
        errors.append(
            FlowValidationIssue(
                code="control-expression-invalid",
                message=str(exc),
                node_id=node.id,
            )
        )


def _validate_input_mapping(node, known_nodes, adjacency, errors) -> None:
    for key, template in node.input_mapping.items():
        if not BINDING_KEY_PATTERN.fullmatch(key):
            errors.append(
                FlowValidationIssue(
                    code="binding-key-invalid",
                    message="Input binding key must be a safe dotted JSON path.",
                    node_id=node.id,
                )
            )
        try:
            references = template_references(template)
        except ExpressionError as exc:
            errors.append(
                FlowValidationIssue(
                    code="binding-expression-invalid",
                    message=str(exc),
                    node_id=node.id,
                )
            )
            continue
        for reference in references:
            if reference[0] != "nodes":
                continue
            source = reference[1]
            if source not in known_nodes:
                errors.append(
                    FlowValidationIssue(
                        code="binding-node-missing",
                        message=f"Input binding references an unknown node: {source}.",
                        node_id=node.id,
                    )
                )
            elif source == node.id or node.id not in _reachable(source, adjacency):
                errors.append(
                    FlowValidationIssue(
                        code="binding-node-not-upstream",
                        message=f"Input binding node is not upstream: {source}.",
                        node_id=node.id,
                    )
                )


def published_snapshot(flow: FlowDefinition, version: int) -> tuple[FlowDefinition, str]:
    definition = flow.model_copy(
        update={
            "version": str(version),
            "status": "published",
            "read_only": True,
            "builtin": False,
            "nodes": [node.model_copy(update={"read_only": True}) for node in flow.nodes],
        }
    )
    return definition, flow_digest(definition)


def flow_digest(flow: FlowDefinition) -> str:
    canonical = json.dumps(
        flow.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _reachable(start: str, adjacency: dict[str, list[str]]) -> set[str]:
    reached: set[str] = set()
    queue = deque([start])
    while queue:
        node_id = queue.popleft()
        if node_id in reached:
            continue
        reached.add(node_id)
        queue.extend(adjacency[node_id])
    return reached


def _duplicates(values, code, message, issues, *, edge=False) -> None:
    seen: set[str] = set()
    reported: set[str] = set()
    for value in values:
        if value in seen and value not in reported:
            kwargs = {"edge_id" if edge else "node_id": value}
            issues.append(FlowValidationIssue(code=code, message=message, **kwargs))
            reported.add(value)
        seen.add(value)
