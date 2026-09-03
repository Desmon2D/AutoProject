from pathlib import Path

from automation_orchestrator.flow_builder import (
    FAILURE_NODE_ID,
    SUCCESS_NODE_ID,
    TRIGGER_NODE_ID,
    builtin_node_types,
    scenario_to_flow,
)
from automation_orchestrator.flow_validation import validate_flow
from automation_orchestrator.scenario_registry import ScenarioRegistry


def test_scenario_is_adapted_to_read_only_flow_with_explicit_terminals():
    registry = ScenarioRegistry(Path(__file__).parents[1] / "scenarios")
    scenario = registry.get("bug-finding")

    flow = scenario_to_flow(scenario)

    assert flow.id == scenario.id
    assert flow.version == scenario.version
    assert flow.builtin is True
    assert flow.read_only is True
    assert flow.start_node == scenario.start_step
    node_ids = {node.id for node in flow.nodes}
    assert {TRIGGER_NODE_ID, SUCCESS_NODE_ID, FAILURE_NODE_ID}.issubset(node_ids)
    assert len(flow.nodes) == len(scenario.steps) + 3
    assert flow.edges[0].source == TRIGGER_NODE_ID
    assert flow.edges[0].target == scenario.start_step
    assert flow.edges[0].kind == "event"
    assert len(flow.edges) == 1 + len(scenario.steps) * 2
    assert all(node.read_only for node in flow.nodes)


def test_flow_layout_is_stable_and_supports_cycles():
    registry = ScenarioRegistry(Path(__file__).parents[1] / "scenarios")
    scenario = registry.get("implement-ticket")

    first = scenario_to_flow(scenario)
    second = scenario_to_flow(scenario)

    assert first == second
    assert len({node.id for node in first.nodes}) == len(first.nodes)
    assert all(edge.source in {node.id for node in first.nodes} for edge in first.edges)
    assert all(edge.target in {node.id for node in first.nodes} for edge in first.edges)
    assert next(node for node in first.nodes if node.id == TRIGGER_NODE_ID).position.x == 40


def test_builtin_node_catalog_covers_scenario_and_virtual_nodes():
    node_types = builtin_node_types()

    assert {item.type for item in node_types} == {
        "trigger",
        "agent",
        "command",
        "review",
        "if",
        "switch",
        "delay",
        "merge",
        "terminal",
    }
    command = next(item for item in node_types if item.type == "command")
    assert command.config_schema["required"] == ["command", "parameters"]
    assert command.config_schema["properties"]["command"]["x-ui-catalog"] == "operations"
    assert command.config_schema["properties"]["parameters"]["x-ui-schema-from"] == {
        "catalog": "operations",
        "selector": "command",
        "field": "input_schema",
    }
    assert command.config_schema["properties"]["parameters"]["type"] == "object"
    assert command.config_schema["properties"]["retry"]["properties"]["max_attempts"] == {
        "type": "integer",
        "title": "Максимум попыток",
        "minimum": 1,
        "maximum": 10,
        "default": 1,
    }
    assert command.outcomes == ["SUCCESS", "FAILURE"]
    agent = next(item for item in node_types if item.type == "agent")
    assert agent.config_schema["properties"]["plugins"]["type"] == "array"
    assert agent.config_schema["properties"]["model"]["x-ui-catalog"] == "models"
    assert agent.config_schema["properties"]["plugins"]["x-ui-catalog"] == "plugins"
    assert agent.config_schema["properties"]["result_contract"]["enum"][-1] == "bug_report"
    trigger = next(item for item in node_types if item.type == "trigger")
    assert trigger.config_schema["properties"]["source"]["title"] == "Источник"
    assert trigger.config_schema["properties"]["source"]["enum"] == [
        "manual",
        "webhook",
        "gitea",
        "plane",
    ]
    assert "push" in trigger.config_schema["properties"]["event"]["x-ui-options-by"][
        "values"
    ]["gitea"]
    delay = next(item for item in node_types if item.type == "delay")
    assert delay.config_schema["properties"]["seconds"]["maximum"] == 86_400


def test_flow_validation_enforces_schema_driven_agent_settings():
    registry = ScenarioRegistry(Path(__file__).parents[1] / "scenarios")
    flow = scenario_to_flow(registry.get("bug-finding"))
    agent = next(node for node in flow.nodes if node.type == "agent")
    broken_agent = agent.model_copy(
        update={
            "config": {
                **agent.config,
                "plugins": ["not a plugin"],
                "timeout_seconds": 0,
                "result_contract": "unknown",
                "retry": {"max_attempts": 0},
            }
        }
    )
    broken = flow.model_copy(
        update={
            "nodes": [broken_agent if node.id == broken_agent.id else node for node in flow.nodes]
        }
    )

    result = validate_flow(broken)

    assert {
        "agent-plugins-invalid",
        "agent-timeout-invalid",
        "agent-result-contract-invalid",
        "retry-policy-invalid",
    }.issubset({issue.code for issue in result.errors})


def test_flow_validation_rejects_trigger_event_outside_source_catalog():
    registry = ScenarioRegistry(Path(__file__).parents[1] / "scenarios")
    flow = scenario_to_flow(registry.get("bug-finding"))
    trigger = next(node for node in flow.nodes if node.type == "trigger")
    broken_trigger = trigger.model_copy(
        update={"config": {"source": "gitea", "event": "issue.testing"}}
    )
    broken = flow.model_copy(
        update={
            "nodes": [
                broken_trigger if node.id == broken_trigger.id else node
                for node in flow.nodes
            ]
        }
    )

    result = validate_flow(broken)

    assert "trigger-event-invalid" in {issue.code for issue in result.errors}


def test_flow_validation_rejects_credential_for_another_provider():
    registry = ScenarioRegistry(Path(__file__).parents[1] / "scenarios")
    flow = scenario_to_flow(registry.get("bug-finding"))
    agent = next(node for node in flow.nodes if node.type == "agent")
    broken_agent = agent.model_copy(
        update={"config": {**agent.config, "credential_id": "openai-default"}}
    )
    broken = flow.model_copy(
        update={
            "nodes": [
                broken_agent if node.id == broken_agent.id else node
                for node in flow.nodes
            ]
        }
    )

    result = validate_flow(broken)

    assert "credential-provider-mismatch" in {issue.code for issue in result.errors}
