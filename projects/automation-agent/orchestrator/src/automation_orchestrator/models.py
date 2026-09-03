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
    node_inputs: dict[str, Any] = Field(default_factory=dict)
    scenario: dict[str, Any] = Field(default_factory=dict)
    previous_steps: list[PreviousStepResult] = Field(default_factory=list, max_length=100)
    review_comments: list[str] = Field(default_factory=list, max_length=100)
    retrieval_summary: dict[str, Any] = Field(default_factory=dict)
    swirl_results: list[dict[str, Any]] = Field(default_factory=list, max_length=100)


class SwirlContentExcerpt(StrictModel):
    heading: str | None = Field(default=None, max_length=500)
    text: str = Field(min_length=1, max_length=4000)
    relevance_score: float = Field(ge=0)
    matched_terms: list[str] = Field(default_factory=list, max_length=40)


class SwirlSearchResult(StrictModel):
    title: str = Field(min_length=1, max_length=1000)
    snippet: str = Field(default="", max_length=2000)
    url: str = Field(min_length=1, max_length=4000)
    source: str = Field(default="unknown", max_length=300)
    document_id: str | None = Field(default=None, max_length=500)
    content: str | None = Field(default=None, max_length=50_000)
    excerpts: list[SwirlContentExcerpt] = Field(default_factory=list, max_length=20)
    content_fetched: bool = False
    content_format: str | None = Field(default=None, max_length=50)
    content_truncated: bool = False
    retrieval_score: float | None = Field(default=None, ge=0)
    matched_queries: list[str] = Field(default_factory=list, max_length=20)
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
    fallback_on_empty: bool = False
    max_fallback_queries: int = Field(default=6, ge=1, le=12)
    expand_query: bool = False
    rank_fusion_k: int = Field(default=60, ge=1, le=200)
    focused_query_weight: float = Field(default=1.5, ge=0.1, le=5)
    fetch_content: bool = False
    max_content_documents: int = Field(default=5, ge=1, le=10)
    max_content_characters: int = Field(default=50_000, ge=1000, le=50_000)
    min_content_documents: int = Field(default=1, ge=1, le=10)
    max_context_characters: int = Field(default=12_000, ge=2000, le=30_000)
    max_chunk_characters: int = Field(default=2000, ge=500, le=4000)
    max_chunks_per_document: int = Field(default=2, ge=1, le=5)
    max_context_results: int = Field(default=8, ge=1, le=20)
    max_snippet_characters: int = Field(default=600, ge=100, le=2000)
    min_chunk_relevance: float = Field(default=1.0, ge=0, le=1000)

    @model_validator(mode="after")
    def validate_query_source(self):
        if (self.query is None) == (self.query_field is None):
            raise ValueError("context_search requires exactly one of query or query_field")
        if self.min_content_documents > self.max_content_documents:
            raise ValueError(
                "min_content_documents cannot exceed max_content_documents"
            )
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
    source_report: list[ContextSourceReport] = Field(default_factory=list)
    character_count: int
    truncated: bool
    digest: str


class ContextSourceReport(StrictModel):
    source: str = Field(min_length=1, max_length=100)
    category: Literal[
        "instructions",
        "requirements",
        "repository",
        "review",
        "history",
        "documentation",
    ]
    available_characters: int = Field(ge=0)
    included_characters: int = Field(ge=0)
    item_count: int = Field(default=1, ge=0)
    truncated: bool = False
    omitted: bool = False


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
    status: Literal[
        "PENDING", "READY", "RUNNING", "WAITING", "COMPLETED", "ERROR", "CANCELLED", "SKIPPED"
    ]
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class StepResult(StrictModel):
    step_id: str
    execution_id: str
    iteration: int
    attempt: int
    execution_status: Literal[
        "PENDING",
        "READY",
        "RUNNING",
        "WAITING",
        "COMPLETED",
        "ERROR",
        "CANCELLED",
        "SKIPPED",
    ]
    outcome: Literal["SUCCESS", "FAILURE"] | None
    inputs: dict[str, Any] = Field(default_factory=dict)
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
    input_mapping: dict[str, str] = Field(default_factory=dict)

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
    credential_id: str | None = Field(default=None, pattern=PLUGIN_PATTERN)
    timeout_seconds: int = Field(default=600, ge=1, le=3600)
    context_search: SwirlContextSearch | None = None
    result_contract: Literal[
        "none",
        "pull_request",
        "implementation_change",
        "test_change",
        "test_execution",
        "markdown_document",
        "bug_report",
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
    provider: Literal["gitea", "plane"] = "gitea"
    decision: Literal["review", "merge"] = "review"
    credential_id: str | None = Field(default=None, pattern=PLUGIN_PATTERN)


class IfScenarioStep(ScenarioStepBase):
    type: Literal["if"]
    condition: str = Field(min_length=1, max_length=4000)


class SwitchScenarioStep(ScenarioStepBase):
    type: Literal["switch"]
    value: str = Field(min_length=1, max_length=4000)
    equals: str | int | float | bool | None


class DelayScenarioStep(ScenarioStepBase):
    type: Literal["delay"]
    seconds: int = Field(ge=0, le=86_400)


class MergeScenarioStep(ScenarioStepBase):
    type: Literal["merge"]
    mode: Literal["any", "all"] = "any"


ScenarioStep = Annotated[
    AgentScenarioStep
    | CommandScenarioStep
    | ReviewScenarioStep
    | IfScenarioStep
    | SwitchScenarioStep
    | DelayScenarioStep
    | MergeScenarioStep,
    Field(discriminator="type"),
]


class ScenarioManifest(StrictModel):
    id: str
    version: str = "1"
    stage: Literal[
        "analysis",
        "development",
        "testing",
        "bug-finding",
        "operations",
        "system",
    ] = "system"
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, min_length=1, max_length=1000)
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


class FlowPosition(StrictModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)


class FlowNode(StrictModel):
    id: str
    type: Literal[
        "trigger",
        "agent",
        "command",
        "review",
        "if",
        "switch",
        "delay",
        "merge",
        "terminal",
    ]
    category: Literal["trigger", "execution", "control", "data", "terminal"]
    title: str
    subtitle: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    input_mapping: dict[str, str] = Field(default_factory=dict)
    position: FlowPosition
    read_only: bool = True


class FlowEdge(StrictModel):
    id: str
    source: str
    source_port: str
    target: str
    label: str
    kind: Literal["event", "transition"]
    outcome: Literal["SUCCESS", "FAILURE"] | None = None


class FlowDefinition(StrictModel):
    id: str
    revision: int = Field(default=0, ge=0)
    version: str
    title: str
    description: str | None = None
    stage: Literal[
        "analysis",
        "development",
        "testing",
        "bug-finding",
        "operations",
        "system",
    ]
    enabled: bool
    builtin: bool = True
    read_only: bool = True
    status: Literal["builtin", "draft", "published"] = "builtin"
    source_scenario_id: str | None = None
    start_node: str
    nodes: list[FlowNode]
    edges: list[FlowEdge]


class FlowNodeType(StrictModel):
    type: Literal[
        "trigger",
        "agent",
        "command",
        "review",
        "if",
        "switch",
        "delay",
        "merge",
        "terminal",
    ]
    category: Literal["trigger", "execution", "control", "data", "terminal"]
    title: str
    description: str
    version: int = Field(default=1, ge=1)
    config_schema: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    outcomes: list[Literal["EVENT", "SUCCESS", "FAILURE"]] = Field(default_factory=list)


class AgentModelDefinition(StrictModel):
    id: str = Field(min_length=1, max_length=200)
    provider: Literal["openai", "openrouter"]
    title: str = Field(min_length=1, max_length=200)
    configured: bool = False
    default: bool = False


class CredentialReference(StrictModel):
    id: str = Field(pattern=PLUGIN_PATTERN)
    provider: Literal["openai", "openrouter", "gitea", "plane"]
    title: str = Field(min_length=1, max_length=200)
    configured: bool = False


class OperationDefinition(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
    version: int = Field(ge=1)
    category: Literal["control", "data", "integration", "testing", "terminal"]
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    outcomes: list[Literal["SUCCESS", "FAILURE"]]
    errors: list[str] = Field(default_factory=list)
    integrations: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    side_effects: bool = False
    idempotency_required: bool = False
    executor: str
    examples: list[dict[str, Any]] = Field(default_factory=list)
    legacy_command_compatible: bool = True


class FlowCreateRequest(StrictModel):
    source_flow_id: str | None = None
    id: str | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    stage: Literal[
        "analysis",
        "development",
        "testing",
        "bug-finding",
        "operations",
        "system",
    ] = "operations"

    @field_validator("source_flow_id", "id")
    @classmethod
    def validate_flow_ids(cls, value: str | None) -> str | None:
        if value is not None and not PLUGIN_PATTERN.fullmatch(value):
            raise ValueError("flow identifiers must be kebab-case")
        return value


class FlowDraftUpdate(StrictModel):
    expected_revision: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    stage: Literal[
        "analysis",
        "development",
        "testing",
        "bug-finding",
        "operations",
        "system",
    ]
    enabled: bool = True
    start_node: str
    nodes: list[FlowNode]
    edges: list[FlowEdge]


class FlowPublishRequest(StrictModel):
    expected_revision: int = Field(ge=1)


class FlowValidationIssue(StrictModel):
    code: str
    message: str
    node_id: str | None = None
    edge_id: str | None = None


class FlowValidationResult(StrictModel):
    valid: bool
    errors: list[FlowValidationIssue] = Field(default_factory=list)
    warnings: list[FlowValidationIssue] = Field(default_factory=list)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class FlowVersion(StrictModel):
    flow_id: str
    version: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    definition: FlowDefinition
    published_at: datetime


class FlowRunRequest(StrictModel):
    version: int | None = Field(default=None, ge=1)
    inputs: dict[str, Any] = Field(default_factory=dict)


class ActivatedFlowEdge(StrictModel):
    activation_id: str
    edge_id: str
    source: str
    target: str
    outcome: Literal["SUCCESS", "FAILURE"] | None = None
    node_run_id: str | None = None
    activated_at: datetime


class FlowNodeRun(StrictModel):
    id: str
    node_id: str
    iteration: int = Field(ge=1)
    attempt: int = Field(ge=1)
    status: Literal[
        "PENDING",
        "READY",
        "RUNNING",
        "WAITING",
        "COMPLETED",
        "ERROR",
        "CANCELLED",
        "SKIPPED",
    ]
    outcome: Literal["SUCCESS", "FAILURE"] | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    data: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    error: StepError | None = None
    status_history: list[StepStatusChange] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class FlowRun(StrictModel):
    id: str
    flow_id: str
    flow_version: int = Field(ge=1)
    flow_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    workflow_id: str
    status: Literal["CREATED", "RUNNING", "WAITING", "COMPLETED", "FAILED", "CANCELLED"]
    outcome: Literal["SUCCESS", "FAILURE"] | None = None
    current_node: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    node_runs: list[FlowNodeRun] = Field(default_factory=list)
    activated_edges: list[ActivatedFlowEdge] = Field(default_factory=list)
    error: StepError | None = None
    created_at: datetime
    updated_at: datetime


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


class FlowTriggerDispatch(StrictModel):
    accepted: bool
    source: str
    event: str
    event_id: str
    flow_runs: list[FlowRun] = Field(default_factory=list)
    reason: str | None = None


class AnalysisRequest(StrictModel):
    request: str = Field(min_length=3, max_length=20_000)
    title: str | None = Field(default=None, min_length=1, max_length=300)

    @field_validator("request", "title", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class BugFindingRequest(StrictModel):
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=300)
    ref: str = Field(default="main", min_length=1, max_length=300)
    scope: str = Field(min_length=3, max_length=20_000)
    symptoms: str | None = Field(default=None, min_length=1, max_length=20_000)
    logs: str | None = Field(default=None, min_length=1, max_length=50_000)
    constraints: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("repository", "ref", "scope", "symptoms", "logs", mode="before")
    @classmethod
    def normalize_bug_finding_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        owner, name = value.split("/", maxsplit=1)
        if owner in {".", ".."} or name in {".", ".."}:
            raise ValueError("invalid repository name")
        return value

    @field_validator("ref")
    @classmethod
    def validate_ref(cls, value: str) -> str:
        if (
            re.fullmatch(r"[A-Za-z0-9._/-]+", value) is None
            or ".." in value
            or "//" in value
            or value.endswith(("/", ".lock"))
        ):
            raise ValueError("invalid Git ref")
        return value

    @field_validator("constraints")
    @classmethod
    def normalize_constraints(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value or len(value) > 1000 for value in normalized):
            raise ValueError("constraints must contain non-empty strings up to 1000 characters")
        return normalized


class IgnoredWebhook(StrictModel):
    accepted: Literal[False] = False
    reason: str = Field(min_length=1, max_length=1000)


class PendingReview(StrictModel):
    step_id: str
    execution_id: str
    iteration: int
    provider: Literal["gitea", "plane"] = "gitea"
    decision: Literal["review", "merge"] = "review"
    inputs: dict[str, Any] = Field(default_factory=dict)
    repository: str | None = Field(default=None, max_length=300)
    pull_index: int | None = Field(default=None, ge=1)
    url: str | None = Field(default=None, max_length=4000)


class PendingDelay(StrictModel):
    step_id: str = Field(min_length=1, max_length=128)
    execution_id: str
    iteration: int = Field(ge=1)
    available_at: datetime
    inputs: dict[str, Any] = Field(default_factory=dict)


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
    scenario_snapshot_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    trigger: TriggerEvent
    status: Literal["CREATED", "RUNNING", "WAITING", "COMPLETED", "FAILED", "CANCELLED"]
    outcome: Literal["SUCCESS", "FAILURE"] | None = None
    current_step: str | None
    iterations: dict[str, int] = Field(default_factory=dict)
    executions: list[StepResult] = Field(default_factory=list)
    review_comments: list[str] = Field(default_factory=list)
    processed_event_ids: list[str] = Field(default_factory=list, max_length=1000)
    pending_review: PendingReview | None = None
    pending_delay: PendingDelay | None = None
    pending_retry: PendingRetry | None = None
    error: StepError | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    deadline_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    cancelled_at: datetime | None = None
