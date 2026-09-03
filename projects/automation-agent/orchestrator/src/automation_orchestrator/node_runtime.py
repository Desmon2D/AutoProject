from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .expression_engine import ExpressionError, resolve_input_mapping, resolve_template
from .models import (
    AgentScenarioStep,
    CommandScenarioStep,
    DelayScenarioStep,
    IfScenarioStep,
    MergeScenarioStep,
    ReviewScenarioStep,
    StepResult,
    SwitchScenarioStep,
    WorkflowInstance,
)
from .workflow_commands import CommandExecutor
from .workflow_errors import WorkflowExecutionError

NodeStep = (
    AgentScenarioStep
    | CommandScenarioStep
    | ReviewScenarioStep
    | IfScenarioStep
    | SwitchScenarioStep
    | DelayScenarioStep
    | MergeScenarioStep
)


class NodeRuntimeError(WorkflowExecutionError):
    pass


@dataclass(frozen=True)
class NodeExecutionInput:
    workflow: WorkflowInstance
    step_id: str
    iteration: int
    attempt: int
    step: NodeStep
    inputs: dict[str, Any]


AgentNodeExecutor = Callable[[NodeExecutionInput], StepResult]


class NodeRuntime:
    """Resolves node bindings and dispatches executable nodes through one contract."""

    def __init__(self, command_executor: CommandExecutor):
        self.command_executor = command_executor

    def prepare(
        self,
        *,
        workflow: WorkflowInstance,
        step_id: str,
        iteration: int,
        attempt: int,
        step: NodeStep,
    ) -> NodeExecutionInput:
        previous = next(
            (
                result
                for result in reversed(workflow.executions)
                if result.step_id == step_id and result.execution_status == "ERROR"
            ),
            None,
        )
        if previous is not None and (
            previous.error is None or previous.error.code != "NODE_INPUT_ERROR"
        ):
            inputs = deepcopy(previous.inputs)
        else:
            try:
                inputs = resolve_input_mapping(
                    step.input_mapping,
                    self.expression_context(workflow),
                )
            except ExpressionError as exc:
                raise NodeRuntimeError(f"node input mapping failed: {exc}") from exc
        return NodeExecutionInput(
            workflow=workflow,
            step_id=step_id,
            iteration=iteration,
            attempt=attempt,
            step=step,
            inputs=inputs,
        )

    def execute(
        self,
        execution: NodeExecutionInput,
        *,
        agent_executor: AgentNodeExecutor,
    ) -> StepResult:
        if isinstance(execution.step, CommandScenarioStep):
            bound_step = execution.step.model_copy(
                update={
                    "parameters": _deep_merge(
                        execution.step.parameters,
                        execution.inputs,
                    )
                }
            )
            result = self.command_executor.execute(
                workflow=execution.workflow,
                step_id=execution.step_id,
                iteration=execution.iteration,
                attempt=execution.attempt,
                step=bound_step,
            )
        elif isinstance(execution.step, AgentScenarioStep):
            result = agent_executor(execution)
        elif isinstance(execution.step, IfScenarioStep):
            result = self._execute_if(execution)
        elif isinstance(execution.step, SwitchScenarioStep):
            result = self._execute_switch(execution)
        elif isinstance(execution.step, MergeScenarioStep):
            result = self._completed(
                execution,
                outcome="SUCCESS",
                data={"summary": f"merge.{execution.step.mode} activated"},
            )
        else:
            raise NodeRuntimeError("waiting nodes require scheduler handling")
        return result.model_copy(update={"inputs": execution.inputs})

    def _execute_if(self, execution: NodeExecutionInput) -> StepResult:
        try:
            value = resolve_template(
                execution.step.condition,
                self.expression_context(execution.workflow),
            )
        except ExpressionError as exc:
            raise NodeRuntimeError(f"if condition failed: {exc}") from exc
        if type(value) is not bool:
            raise NodeRuntimeError("if condition must evaluate to a boolean")
        return self._completed(
            execution,
            outcome="SUCCESS" if value else "FAILURE",
            data={"summary": "if condition evaluated", "condition": value},
        )

    def _execute_switch(self, execution: NodeExecutionInput) -> StepResult:
        try:
            value = resolve_template(
                execution.step.value,
                self.expression_context(execution.workflow),
            )
        except ExpressionError as exc:
            raise NodeRuntimeError(f"switch value failed: {exc}") from exc
        matched = value == execution.step.equals and type(value) is type(execution.step.equals)
        return self._completed(
            execution,
            outcome="SUCCESS" if matched else "FAILURE",
            data={
                "summary": "switch value matched" if matched else "switch used default",
                "value": value,
                "equals": execution.step.equals,
                "matched": matched,
            },
        )

    @staticmethod
    def _completed(
        execution: NodeExecutionInput,
        *,
        outcome: str,
        data: dict[str, Any],
    ) -> StepResult:
        return StepResult(
            step_id=execution.step_id,
            execution_id=CommandExecutor.execution_id(
                execution.workflow.id,
                execution.step_id,
                execution.iteration,
                execution.attempt,
            ),
            iteration=execution.iteration,
            attempt=execution.attempt,
            execution_status="COMPLETED",
            outcome=outcome,
            inputs=execution.inputs,
            data=data,
        )

    @staticmethod
    def expression_context(workflow: WorkflowInstance) -> dict[str, Any]:
        trigger = dict(workflow.trigger.data)
        inputs = dict(trigger)
        inputs.pop("flow", None)
        nodes: dict[str, dict[str, Any]] = {}
        for result in workflow.executions:
            if result.execution_status != "COMPLETED":
                continue
            nodes[result.step_id] = {
                "outcome": result.outcome,
                "data": result.data,
                "artifacts": [
                    artifact.model_dump(mode="json") for artifact in result.artifacts
                ],
                "error": result.error.model_dump(mode="json") if result.error else None,
            }
        return {"inputs": inputs, "trigger": trigger, "nodes": nodes}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged
