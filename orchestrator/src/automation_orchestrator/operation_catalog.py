from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field, ValidationError

from .models import OperationDefinition, StrictModel

STEP_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"


class _SummaryParameters(StrictModel):
    summary: str | None = Field(default=None, min_length=1, max_length=4000)
    data: dict[str, Any] = Field(default_factory=dict)


class _FailureReportParameters(StrictModel):
    summary: str | None = Field(default=None, min_length=1, max_length=4000)


class _AuthorStepParameters(StrictModel):
    author_step: str = Field(pattern=STEP_ID_PATTERN)


class _RewriteParameters(_AuthorStepParameters):
    max_iterations: int = Field(ge=1, le=10)


class _ClassifyParameters(StrictModel):
    executor_step: str = Field(pattern=STEP_ID_PATTERN)


class _PullRequestParameters(_AuthorStepParameters):
    executor_step: str = Field(pattern=STEP_ID_PATTERN)
    summary: str | None = Field(default=None, min_length=1, max_length=4000)


class _PlaneSyncParameters(StrictModel):
    recommendation: str = Field(min_length=1, max_length=100)
    summary: str | None = Field(default=None, min_length=1, max_length=4000)


@dataclass(frozen=True)
class _RegisteredOperation:
    definition: OperationDefinition
    parameters_model: type[StrictModel]


def _object_schema(properties: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": True,
    }


def _definition(
    operation_id: str,
    *,
    category: Literal["control", "data", "integration", "testing", "terminal"],
    title: str,
    description: str,
    model: type[StrictModel],
    output_schema: dict[str, Any] | None = None,
    errors: list[str] | None = None,
    integrations: list[str] | None = None,
    capabilities: list[str] | None = None,
    side_effects: bool = False,
    idempotency_required: bool = False,
    examples: list[dict[str, Any]] | None = None,
) -> _RegisteredOperation:
    return _RegisteredOperation(
        definition=OperationDefinition(
            id=operation_id,
            version=1,
            category=category,
            title=title,
            description=description,
            input_schema=model.model_json_schema(),
            output_schema=output_schema or _object_schema(),
            outcomes=["SUCCESS", "FAILURE"],
            errors=errors or [],
            integrations=integrations or [],
            capabilities=capabilities or [],
            side_effects=side_effects,
            idempotency_required=idempotency_required,
            executor=f"workflow-command:{operation_id}",
            examples=examples or [],
        ),
        parameters_model=model,
    )


_OPERATIONS = (
    _definition(
        "complete",
        category="terminal",
        title="Complete workflow",
        description="Completes a workflow with a successful business outcome.",
        model=_SummaryParameters,
        examples=[{"summary": "Workflow completed"}],
    ),
    _definition(
        "fail",
        category="terminal",
        title="Fail workflow",
        description="Completes a workflow with an unsuccessful business outcome.",
        model=_SummaryParameters,
        examples=[{"summary": "Workflow rejected"}],
    ),
    _definition(
        "store_failure_report",
        category="data",
        title="Store failure report",
        description="Copies the latest business failure into structured workflow data.",
        model=_FailureReportParameters,
        output_schema=_object_schema({"failed_step": {"type": ["string", "null"]}}),
    ),
    _definition(
        "allow_test_rewrite",
        category="control",
        title="Allow test rewrite",
        description="Bounds the number of test-authoring repair iterations.",
        model=_RewriteParameters,
    ),
    _definition(
        "allow_bug_rewrite",
        category="control",
        title="Allow bug report rewrite",
        description="Bounds the number of bug-report repair iterations.",
        model=_RewriteParameters,
    ),
    _definition(
        "execute_test_change",
        category="testing",
        title="Run authored tests",
        description="Runs the exact authored test commit in the deterministic test runner.",
        model=_AuthorStepParameters,
        output_schema=_object_schema({"test_report": {"type": "object"}}),
        errors=["TEST_CODE_ERROR", "TEST_RUNNER_ERROR"],
        capabilities=["python", "node"],
    ),
    _definition(
        "verify_bug_report",
        category="testing",
        title="Verify bug reproducers",
        description="Independently executes every reproducer from a structured bug report.",
        model=_AuthorStepParameters,
        output_schema=_object_schema({"bug_verification": {"type": "object"}}),
        errors=["BUG_REPRODUCER_INVALID", "TEST_RUNNER_ERROR"],
        capabilities=["python", "node"],
    ),
    _definition(
        "classify_test_run",
        category="control",
        title="Classify test run",
        description="Routes a validated test report to pass or product-failure outcomes.",
        model=_ClassifyParameters,
        output_schema=_object_schema({"test_report": {"type": "object"}}),
    ),
    _definition(
        "create_final_pull_request",
        category="integration",
        title="Create validated pull request",
        description="Creates the final pull request only for the exact passing commit.",
        model=_PullRequestParameters,
        output_schema=_object_schema({"pull_request": {"type": "object"}}),
        errors=["GITEA_OPERATION_FAILED"],
        integrations=["gitea"],
        capabilities=["git"],
        side_effects=True,
        idempotency_required=True,
    ),
    _definition(
        "sync_plane_issue",
        category="integration",
        title="Update Plane issue",
        description="Writes an idempotent workflow result and lifecycle state to Plane.",
        model=_PlaneSyncParameters,
        output_schema=_object_schema({"plane_sync": {"type": "object"}}),
        errors=["PLANE_OPERATION_FAILED"],
        integrations=["plane"],
        side_effects=True,
        idempotency_required=True,
    ),
)
_BY_ID = {item.definition.id: item for item in _OPERATIONS}


def builtin_operations() -> list[OperationDefinition]:
    return [item.definition for item in _OPERATIONS]


def operation_ids() -> frozenset[str]:
    return frozenset(_BY_ID)


def get_operation(operation_id: str) -> OperationDefinition | None:
    registered = _BY_ID.get(operation_id)
    return registered.definition if registered is not None else None


def validate_operation_parameters(
    operation_id: str,
    parameters: Any,
    *,
    bound_parameters: set[str] | None = None,
) -> list[str]:
    registered = _BY_ID.get(operation_id)
    if registered is None:
        return [f"unknown operation: {operation_id}"]
    try:
        registered.parameters_model.model_validate(parameters)
    except ValidationError as exc:
        messages: list[str] = []
        for issue in exc.errors(include_url=False):
            root = str(issue["loc"][0]) if issue["loc"] else ""
            if bound_parameters and root in bound_parameters:
                continue
            path = ".".join(str(part) for part in issue["loc"])
            prefix = f"parameters.{path}" if path else "parameters"
            messages.append(f"{prefix}: {issue['msg']}")
        return messages
    return []
