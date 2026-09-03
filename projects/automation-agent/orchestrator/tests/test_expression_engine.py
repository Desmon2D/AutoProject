from pathlib import Path

import pytest

from automation_orchestrator.expression_engine import (
    ExpressionError,
    evaluate_expression,
    resolve_input_mapping,
    resolve_template,
    template_references,
)
from automation_orchestrator.flow_builder import scenario_to_flow
from automation_orchestrator.flow_validation import validate_flow
from automation_orchestrator.scenario_registry import ScenarioRegistry


def _context():
    return {
        "inputs": {"limit": 2, "labels": ["bug", "urgent"]},
        "trigger": {"repository": "harnes/payments-api"},
        "nodes": {
            "find-bugs": {
                "outcome": "SUCCESS",
                "data": {"findings": [{"id": "BUG-001"}]},
            }
        },
    }


def test_expression_engine_reads_hyphenated_node_paths_and_boolean_expressions():
    context = _context()

    assert evaluate_expression("trigger.repository", context) == "harnes/payments-api"
    assert evaluate_expression("nodes.find-bugs.data.findings", context) == [
        {"id": "BUG-001"}
    ]
    assert evaluate_expression(
        "length(inputs.labels) >= inputs.limit and exists(nodes.find-bugs.data.findings)",
        context,
    ) is True
    assert evaluate_expression("exists(nodes.find-bugs.data.missing)", context) is False


def test_templates_preserve_native_values_and_interpolate_scalars():
    context = _context()

    assert resolve_template("${{ inputs.labels }}", context) == ["bug", "urgent"]
    assert resolve_template("repo=${{ trigger.repository }}", context) == (
        "repo=harnes/payments-api"
    )
    assert resolve_input_mapping(
        {
            "repository": "${{ trigger.repository }}",
            "findings": "${{ nodes.find-bugs.data.findings }}",
            "options.limit": "${{ inputs.limit }}",
        },
        context,
    ) == {
        "repository": "harnes/payments-api",
        "findings": [{"id": "BUG-001"}],
        "options": {"limit": 2},
    }


def test_expression_engine_rejects_code_execution_and_malformed_templates():
    with pytest.raises(ExpressionError, match="unknown expression root"):
        evaluate_expression("__import__('os')", _context())
    with pytest.raises(ExpressionError, match="unterminated"):
        template_references("${{ trigger.repository")
    with pytest.raises(ExpressionError, match="path conflicts"):
        resolve_input_mapping(
            {"data": "${{ inputs }}", "data.value": "${{ inputs.limit }}"},
            _context(),
        )


def test_flow_validation_checks_binding_syntax_and_node_references():
    registry = ScenarioRegistry(Path(__file__).parents[1] / "scenarios")
    flow = scenario_to_flow(registry.get("bug-finding"))
    verify = next(node for node in flow.nodes if node.id == "verify-reproducers")
    valid_verify = verify.model_copy(
        update={
            "config": {**verify.config, "parameters": {}},
            "input_mapping": {
                "author_step": "${{ nodes.find-bugs.data.author_step }}",
            }
        }
    )
    valid = flow.model_copy(
        update={
            "nodes": [valid_verify if node.id == verify.id else node for node in flow.nodes]
        }
    )

    assert validate_flow(valid).valid is True

    invalid_verify = valid_verify.model_copy(
        update={"input_mapping": {"report": "${{ nodes.unknown.data }}"}}
    )
    invalid = flow.model_copy(
        update={
            "nodes": [invalid_verify if node.id == verify.id else node for node in flow.nodes]
        }
    )
    result = validate_flow(invalid)

    assert result.valid is False
    assert "binding-node-missing" in {issue.code for issue in result.errors}
