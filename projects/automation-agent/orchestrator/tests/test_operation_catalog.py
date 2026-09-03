from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation_orchestrator.api import create_app
from automation_orchestrator.context_builder import ContextBuilder
from automation_orchestrator.flow_builder import scenario_to_flow
from automation_orchestrator.flow_validation import validate_flow
from automation_orchestrator.models import CommandScenarioStep, TriggerEvent, WorkflowInstance
from automation_orchestrator.operation_catalog import (
    builtin_operations,
    operation_ids,
    validate_operation_parameters,
)
from automation_orchestrator.sandbox_manager import SandboxManager
from automation_orchestrator.scenario_registry import ScenarioRegistry
from automation_orchestrator.service import AgentService
from automation_orchestrator.workflow_commands import ALLOWED_WORKFLOW_COMMANDS, CommandExecutor
from automation_orchestrator.workflow_errors import WorkflowExecutionError


def test_operation_catalog_is_the_legacy_command_source_of_truth():
    operations = builtin_operations()

    assert operation_ids() == ALLOWED_WORKFLOW_COMMANDS
    assert {item.id for item in operations} == operation_ids()
    assert all(item.version == 1 for item in operations)
    assert all(item.input_schema.get("type") == "object" for item in operations)
    assert all(item.output_schema.get("type") == "object" for item in operations)
    assert next(item for item in operations if item.id == "sync_plane_issue").side_effects
    assert next(
        item for item in operations if item.id == "create_final_pull_request"
    ).idempotency_required


def test_operation_parameter_contract_rejects_invalid_or_unknown_fields():
    assert validate_operation_parameters(
        "allow_test_rewrite", {"author_step": "write-tests", "max_iterations": 2}
    ) == []
    invalid = validate_operation_parameters(
        "allow_test_rewrite",
        {"author_step": "write-tests", "max_iterations": 0, "shell": "unsafe"},
    )

    assert any("max_iterations" in message for message in invalid)
    assert any("shell" in message for message in invalid)


def test_every_builtin_scenario_uses_valid_typed_operation_parameters():
    registry = ScenarioRegistry(Path(__file__).parents[1] / "scenarios")

    for scenario in registry.list():
        result = validate_flow(scenario_to_flow(scenario))
        assert result.valid, (scenario.id, result.errors)


def test_flow_validation_rejects_invalid_operation_parameters():
    registry = ScenarioRegistry(Path(__file__).parents[1] / "scenarios")
    flow = scenario_to_flow(registry.get("test-ticket"))
    node = next(item for item in flow.nodes if item.id == "allow-test-rewrite")
    broken_node = node.model_copy(
        update={
            "config": {
                **node.config,
                "parameters": {"author_step": "write-tests", "max_iterations": 0},
            }
        }
    )
    broken = flow.model_copy(
        update={
            "nodes": [broken_node if item.id == node.id else item for item in flow.nodes]
        }
    )

    result = validate_flow(broken)

    assert result.valid is False
    assert "command-parameters-invalid" in {item.code for item in result.errors}


def test_operation_catalog_api_exposes_schema_and_execution_metadata(
    image_resolver, tmp_path: Path
):
    service = AgentService(ContextBuilder(), image_resolver, SandboxManager(tmp_path))
    client = TestClient(create_app(service))

    response = client.get("/v1/operations")

    assert response.status_code == 200
    operations = response.json()
    sync = next(item for item in operations if item["id"] == "sync_plane_issue")
    assert sync["integrations"] == ["plane"]
    assert sync["input_schema"]["required"] == ["recommendation"]
    assert sync["executor"] == "workflow-command:sync_plane_issue"


def test_legacy_executor_enforces_the_same_operation_contract():
    workflow = WorkflowInstance(
        id="flow-operation-contract",
        scenario_id="manual-review",
        scenario_version="1",
        trigger=TriggerEvent(source="manual", event="test", event_id="operation-contract"),
        status="RUNNING",
        current_step="rewrite",
    )
    step = CommandScenarioStep(
        type="command",
        command="allow_test_rewrite",
        parameters={"author_step": "write-tests", "max_iterations": 0},
        transitions={"SUCCESS": None, "FAILURE": None},
    )

    with pytest.raises(WorkflowExecutionError, match="command parameters are invalid"):
        CommandExecutor().execute(
            workflow=workflow,
            step_id="rewrite",
            iteration=1,
            attempt=1,
            step=step,
        )
