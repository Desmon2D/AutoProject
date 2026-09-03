from __future__ import annotations

from pydantic import ValidationError

from .flow_validation import validate_flow
from .models import (
    AgentScenarioStep,
    CommandScenarioStep,
    DelayScenarioStep,
    FlowDefinition,
    IfScenarioStep,
    MergeScenarioStep,
    ReviewScenarioStep,
    ScenarioManifest,
    ScenarioTrigger,
    SwitchScenarioStep,
)


class FlowCompilationError(ValueError):
    pass


def compile_flow(flow: FlowDefinition) -> ScenarioManifest:
    validation = validate_flow(flow)
    if not validation.valid:
        codes = ", ".join(issue.code for issue in validation.errors)
        raise FlowCompilationError(f"flow is not executable: {codes}")
    if not flow.enabled:
        raise FlowCompilationError("flow is disabled")

    nodes = {node.id: node for node in flow.nodes}
    outgoing = {(edge.source, edge.source_port): edge for edge in flow.edges}
    trigger_node = next(node for node in flow.nodes if node.type == "trigger")
    steps = {}
    step_models = {
        "agent": AgentScenarioStep,
        "command": CommandScenarioStep,
        "review": ReviewScenarioStep,
        "if": IfScenarioStep,
        "switch": SwitchScenarioStep,
        "delay": DelayScenarioStep,
        "merge": MergeScenarioStep,
    }
    try:
        for node in flow.nodes:
            if node.type not in step_models:
                continue
            transitions: dict[str, str | None] = {}
            for outcome in ("SUCCESS", "FAILURE"):
                edge = outgoing[(node.id, outcome)]
                target = nodes[edge.target]
                if target.type == "terminal":
                    if target.config.get("outcome") != outcome:
                        raise FlowCompilationError(
                            f"edge {edge.id} routes {outcome} to a different terminal outcome"
                        )
                    transitions[outcome] = None
                else:
                    transitions[outcome] = target.id
            step_type = step_models[node.type]
            steps[node.id] = step_type.model_validate(
                {
                    "type": node.type,
                    "transitions": transitions,
                    "input_mapping": node.input_mapping,
                    **node.config,
                }
            )
        return ScenarioManifest(
            id=flow.id,
            version=flow.version,
            stage=flow.stage,
            title=flow.title,
            description=flow.description,
            trigger=ScenarioTrigger.model_validate(trigger_node.config),
            start_step=flow.start_node,
            steps=steps,
            enabled=True,
        )
    except (KeyError, ValidationError) as exc:
        raise FlowCompilationError(f"flow cannot be compiled: {exc}") from exc
