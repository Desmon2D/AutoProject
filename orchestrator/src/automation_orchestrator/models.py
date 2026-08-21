from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PLUGIN_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
COMMAND_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
ENVIRONMENT_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArtifactRef(StrictModel):
    type: str = Field(min_length=1, max_length=100)
    uri: str = Field(min_length=1, max_length=2000)
    summary: str | None = Field(default=None, max_length=2000)


class ArtifactRecord(StrictModel):
    execution_id: str
    path: str = Field(min_length=1, max_length=2000)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime
    expires_at: datetime | None = None


class AuditEvent(StrictModel):
    id: int = Field(ge=1)
    occurred_at: datetime
    actor: str = Field(min_length=1, max_length=200)
    action: str = Field(min_length=1, max_length=200)
    resource_type: str = Field(min_length=1, max_length=100)
    resource_id: str | None = Field(default=None, max_length=200)
    outcome: Literal["SUCCESS", "DENIED", "ERROR"]
    request_id: str | None = Field(default=None, max_length=200)
    source_ip: str | None = Field(default=None, max_length=200)
    details: dict[str, Any] = Field(default_factory=dict)


class ArtifactCleanupResult(StrictModel):
    examined: int = Field(ge=0)
    removed_records: int = Field(ge=0)
    removed_files: int = Field(ge=0)
    failed: list[str] = Field(default_factory=list, max_length=100)


class PreviousStepResult(StrictModel):
    step_id: str = Field(min_length=1, max_length=128)
    execution_status: str = Field(min_length=1, max_length=40)
    outcome: Literal["SUCCESS", "FAILURE"] | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ArtifactRef] = Field(default_factory=list, max_length=100)


class WorkflowContext(StrictModel):
    trigger_data: dict[str, Any] = Field(default_factory=dict)
    scenario: dict[str, Any] = Field(default_factory=dict)
    previous_steps: list[PreviousStepResult] = Field(default_factory=list, max_length=100)
    review_comments: list[str] = Field(default_factory=list, max_length=100)
    swirl_results: list[dict[str, Any]] = Field(default_factory=list, max_length=100)


class SwirlSearchResult(StrictModel):
    title: str = Field(min_length=1, max_length=1000)
    snippet: str = Field(default="", max_length=2000)
    url: str = Field(min_length=1, max_length=4000)
    source: str = Field(default="unknown", max_length=300)
    updated_at: str | None = Field(default=None, max_length=100)
    score: float | None = None


class SwirlSearchResponse(StrictModel):
    query: str = Field(min_length=1, max_length=2000)
    search_id: str | None = Field(default=None, max_length=200)
    results: list[SwirlSearchResult] = Field(default_factory=list, max_length=50)


class SwirlContextSearch(StrictModel):
    query: str | None = Field(default=None, min_length=1, max_length=2000)
    query_field: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,200}$")
    providers: list[str] = Field(default_factory=list, max_length=20)
    max_results: int = Field(default=8, ge=1, le=20)

    @model_validator(mode="after")
    def validate_query_source(self):
        if (self.query is None) == (self.query_field is None):
            raise ValueError("context_search requires exactly one of query or query_field")
        return self


class AgentStep(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    prompt: str = Field(min_length=1, max_length=20_000)
    plugins: list[str] = Field(default_factory=list, max_length=32)
    provider: Literal["openai", "openrouter"] = "openai"
    model: str = Field(min_length=1, max_length=200)
    timeout_seconds: int = Field(default=600, ge=1, le=3600)

    @field_validator("plugins")
    @classmethod
    def validate_plugins(cls, plugins: list[str]) -> list[str]:
        if len(set(plugins)) != len(plugins):
            raise ValueError("plugins must be unique")
        if any(not PLUGIN_PATTERN.fullmatch(plugin) for plugin in plugins):
            raise ValueError("plugin names must be kebab-case")
        return plugins


class AgentRunRequest(StrictModel):
    execution_id: str
    workflow_id: str
    iteration: int = Field(default=1, ge=1)
    attempt: int = Field(default=1, ge=1)
    step: AgentStep
    context: WorkflowContext = Field(default_factory=WorkflowContext)

    @field_validator("execution_id", "workflow_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not IDENTIFIER_PATTERN.fullmatch(value):
            raise ValueError("identifier contains unsupported characters")
        return value


class BuiltContext(StrictModel):
    prompt: str
    included_sources: list[str]
    character_count: int
    truncated: bool
    digest: str


class PluginManifest(StrictModel):
    name: str
    version: str
    description: str
    npm_package: str
    requires_capabilities: list[str] = Field(default_factory=list)
    entrypoint: str = "index.js"
    inject: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    required_environment: list[str] = Field(default_factory=list)
    source_dir: str | None = None
    built_into_image: bool = False
    mandatory: bool = False
    enabled: bool = True
    unavailable_reason: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not PLUGIN_PATTERN.fullmatch(value):
            raise ValueError("plugin name must be kebab-case")
        return value

    @field_validator("requires_capabilities")
    @classmethod
    def validate_capabilities(cls, capabilities: list[str]) -> list[str]:
        if len(set(capabilities)) != len(capabilities):
            raise ValueError("plugin capabilities must be unique")
        if any(not PLUGIN_PATTERN.fullmatch(item) for item in capabilities):
            raise ValueError("invalid plugin capability")
        return capabilities

    @field_validator("required_environment")
    @classmethod
    def validate_environment(cls, names: list[str]) -> list[str]:
        if len(set(names)) != len(names):
            raise ValueError("plugin environment names must be unique")
        if any(not ENVIRONMENT_PATTERN.fullmatch(name) for name in names):
            raise ValueError("invalid plugin environment name")
        return names


class SkillManifest(StrictModel):
    name: str
    version: str
    description: str
    requires_capabilities: list[str] = Field(default_factory=list)
    requires_commands: list[str] = Field(default_factory=list)
    skill_file: str
    enabled: bool = True
    unavailable_reason: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not PLUGIN_PATTERN.fullmatch(value):
            raise ValueError("skill name must be kebab-case")
        return value

    @field_validator("requires_commands")
    @classmethod
    def validate_commands(cls, commands: list[str]) -> list[str]:
        if any(not COMMAND_PATTERN.fullmatch(command) for command in commands):
            raise ValueError("invalid required command")
        return commands

    @field_validator("requires_capabilities")
    @classmethod
    def validate_capabilities(cls, capabilities: list[str]) -> list[str]:
        if len(set(capabilities)) != len(capabilities):
            raise ValueError("skill capabilities must be unique")
        if any(not PLUGIN_PATTERN.fullmatch(item) for item in capabilities):
            raise ValueError("invalid skill capability")
        return capabilities


class CapabilityManifest(StrictModel):
    name: str
    version: str
    description: str
    commands: list[str] = Field(default_factory=list)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not PLUGIN_PATTERN.fullmatch(value):
            raise ValueError("capability name must be kebab-case")
        return value

    @field_validator("commands")
    @classmethod
    def validate_commands(cls, commands: list[str]) -> list[str]:
        if any(not COMMAND_PATTERN.fullmatch(command) for command in commands):
            raise ValueError("invalid capability command")
        return commands


class ImageProfileManifest(StrictModel):
    name: str
    image: str
    harness_version: str
    capabilities: list[str]
    plugins: list[str]
    build_target: str
    enabled: bool = True

    @field_validator("name", "build_target")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not PLUGIN_PATTERN.fullmatch(value):
            raise ValueError("image profile fields must be kebab-case")
        return value

    @field_validator("capabilities", "plugins")
    @classmethod
    def validate_items(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("image profile items must be unique")
        if any(not PLUGIN_PATTERN.fullmatch(value) for value in values):
            raise ValueError("invalid image profile item")
        return values


class ImageSpec(StrictModel):
    profile: str
    base_image: str
    image: str
    harness_version: str
    capabilities: list[str]
    plugins: list[str]
    digest: str
    requires_build: bool


class PluginResolution(StrictModel):
    plugins: list[PluginManifest]


class SkillResolution(StrictModel):
    skills: list[SkillManifest]
    required_capabilities: list[str]
    required_commands: list[str]


class AgentImageResolution(StrictModel):
    image: str
    image_spec: ImageSpec
    plugins: list[PluginManifest]
    skills: list[SkillManifest]
    required_commands: list[str]


class PreparedAgentStep(StrictModel):
    execution_id: str
    image: str
    image_spec: ImageSpec
    plugins: list[str]
    required_commands: list[str]
    context: BuiltContext
    task: dict[str, Any]


class StepError(StrictModel):
    code: str
    message: str
    retryable: bool


class StepStatusChange(StrictModel):
    status: Literal["PENDING", "READY", "RUNNING", "WAITING", "COMPLETED", "ERROR", "CANCELLED"]
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class StepResult(StrictModel):
    step_id: str
    execution_id: str
    iteration: int
    attempt: int
    execution_status: Literal[
        "PENDING", "READY", "RUNNING", "WAITING", "COMPLETED", "ERROR", "CANCELLED"
    ]
    outcome: Literal["SUCCESS", "FAILURE"] | None
    data: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    error: StepError | None = None
    status_history: list[StepStatusChange] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_outcome(self):
        if self.execution_status == "COMPLETED" and self.outcome is None:
            raise ValueError("completed step execution requires an outcome")
        if self.execution_status != "COMPLETED" and self.outcome is not None:
            raise ValueError("only a completed step execution may have an outcome")
        return self


class SandboxResult(StrictModel):
    job_id: str
    status: Literal["success", "failure", "error"]
    summary: str
    provider: str | None = None
    model: str | None = None
    duration_ms: int | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    error: dict[str, Any] | None = None


class RetryPolicy(StrictModel):
    max_attempts: int = Field(default=1, ge=1, le=10)
    delay_seconds: int = Field(default=5, ge=0, le=3600)
    backoff: Literal["fixed", "exponential"] = "exponential"
    max_delay_seconds: int = Field(default=300, ge=0, le=86_400)

    def delay_for(self, attempt: int) -> int:
        delay = self.delay_seconds
        if self.backoff == "exponential":
            delay *= 2 ** max(0, attempt - 1)
        return min(delay, self.max_delay_seconds)


class ScenarioTrigger(StrictModel):
    source: str = Field(min_length=1, max_length=100)
    event: str = Field(min_length=1, max_length=200)


class ScenarioStepBase(StrictModel):
    transitions: dict[str, str | None]
    retry: RetryPolicy = Field(default_factory=RetryPolicy)

    @field_validator("transitions")
    @classmethod
    def validate_transitions(cls, transitions: dict[str, str | None]):
        if set(transitions) != {"SUCCESS", "FAILURE"}:
            raise ValueError("step transitions must define SUCCESS and FAILURE")
        for target in transitions.values():
            if target is not None and not PLUGIN_PATTERN.fullmatch(target):
                raise ValueError("invalid transition target")
        return transitions


class AgentScenarioStep(ScenarioStepBase):
    type: Literal["agent"]
    prompt: str = Field(min_length=1, max_length=20_000)
    plugins: list[str] = Field(default_factory=list, max_length=32)
    provider: Literal["openai", "openrouter"] = "openai"
    model: str = Field(min_length=1, max_length=200)
    timeout_seconds: int = Field(default=600, ge=1, le=3600)
    context_search: SwirlContextSearch | None = None
    result_contract: Literal[
        "none",
        "pull_request",
        "implementation_change",
        "test_change",
        "test_execution",
    ] = "none"

    @field_validator("plugins")
    @classmethod
    def validate_extension_names(cls, names: list[str]) -> list[str]:
        if len(set(names)) != len(names):
            raise ValueError("scenario extension names must be unique")
        if any(not PLUGIN_PATTERN.fullmatch(name) for name in names):
            raise ValueError("scenario extension names must be kebab-case")
        return names


class CommandScenarioStep(ScenarioStepBase):
    type: Literal["command"]
    command: str = Field(min_length=1, max_length=100)
    parameters: dict[str, Any] = Field(default_factory=dict)


class ReviewScenarioStep(ScenarioStepBase):
    type: Literal["review"]
    provider: Literal["gitea"] = "gitea"
    decision: Literal["review", "merge"] = "review"


ScenarioStep = Annotated[
    AgentScenarioStep | CommandScenarioStep | ReviewScenarioStep,
    Field(discriminator="type"),
]


class ScenarioManifest(StrictModel):
    id: str
    version: str = "1"
    trigger: ScenarioTrigger
    start_step: str
    steps: dict[str, ScenarioStep]
    timeout_seconds: int = Field(default=86_400, ge=1, le=604_800)
    enabled: bool = True

    @field_validator("id", "start_step")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not PLUGIN_PATTERN.fullmatch(value):
            raise ValueError("scenario and step identifiers must be kebab-case")
        return value

    @model_validator(mode="after")
    def validate_graph(self):
        if self.start_step not in self.steps:
            raise ValueError("start_step is missing from scenario steps")
        for step_id, step in self.steps.items():
            if not PLUGIN_PATTERN.fullmatch(step_id):
                raise ValueError("scenario step identifiers must be kebab-case")
            unknown = {
                target
                for target in step.transitions.values()
                if target is not None and target not in self.steps
            }
            if unknown:
                raise ValueError(f"step {step_id} has unknown transitions: {sorted(unknown)}")
        return self


class TriggerEvent(StrictModel):
    source: str = Field(min_length=1, max_length=100)
    event: str = Field(min_length=1, max_length=200)
    event_id: str
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_id")
    @classmethod
    def validate_event_id(cls, value: str) -> str:
        if not IDENTIFIER_PATTERN.fullmatch(value):
            raise ValueError("invalid event_id")
        return value


class IgnoredWebhook(StrictModel):
    accepted: Literal[False] = False
    reason: str = Field(min_length=1, max_length=1000)


class PendingReview(StrictModel):
    step_id: str
    execution_id: str
    iteration: int
    provider: Literal["gitea"] = "gitea"
    decision: Literal["review", "merge"] = "review"
    repository: str | None = Field(default=None, max_length=300)
    pull_index: int | None = Field(default=None, ge=1)
    url: str | None = Field(default=None, max_length=4000)


class ReviewDecision(StrictModel):
    outcome: Literal["SUCCESS", "FAILURE"]
    comments: list[str] = Field(default_factory=list, max_length=100)
    external_event_id: str | None = None
    external_url: str | None = Field(default=None, max_length=4000)

    @field_validator("external_event_id")
    @classmethod
    def validate_external_event_id(cls, value: str | None) -> str | None:
        if value is not None and not IDENTIFIER_PATTERN.fullmatch(value):
            raise ValueError("invalid external_event_id")
        return value


class WorkflowActionRequest(StrictModel):
    reason: str = Field(default="Requested by user", min_length=1, max_length=2000)


class PendingRetry(StrictModel):
    step_id: str = Field(min_length=1, max_length=128)
    iteration: int = Field(ge=1)
    next_attempt: int = Field(ge=2, le=10)
    available_at: datetime


class WorkflowInstance(StrictModel):
    id: str
    scenario_id: str
    scenario_version: str
    trigger: TriggerEvent
    status: Literal["CREATED", "RUNNING", "WAITING", "COMPLETED", "FAILED", "CANCELLED"]
    outcome: Literal["SUCCESS", "FAILURE"] | None = None
    current_step: str | None
    iterations: dict[str, int] = Field(default_factory=dict)
    executions: list[StepResult] = Field(default_factory=list)
    review_comments: list[str] = Field(default_factory=list)
    processed_event_ids: list[str] = Field(default_factory=list, max_length=1000)
    pending_review: PendingReview | None = None
    pending_retry: PendingRetry | None = None
    error: StepError | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    deadline_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    cancelled_at: datetime | None = None
