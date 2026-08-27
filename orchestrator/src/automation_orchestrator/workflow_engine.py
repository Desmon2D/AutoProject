from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any

from .audit_store import AuditStore
from .capability_registry import CapabilityResolutionError
from .image_builder import ImageBuildError
from .image_registry import ImageResolutionError
from .job_store import IdempotencyConflict
from .models import (
    AgentRunRequest,
    AgentScenarioStep,
    AgentStep,
    CommandScenarioStep,
    DelayScenarioStep,
    PendingDelay,
    PendingRetry,
    PendingReview,
    PreviousStepResult,
    ReviewDecision,
    ReviewScenarioStep,
    ScenarioManifest,
    StepError,
    StepResult,
    StepStatusChange,
    TriggerEvent,
    WorkflowContext,
    WorkflowInstance,
)
from .node_runtime import NodeExecutionInput, NodeRuntime, NodeRuntimeError
from .plugin_registry import PluginResolutionError
from .sandbox_manager import SandboxExecutionError
from .scenario_registry import ScenarioRegistry
from .service import AgentService
from .skill_registry import SkillResolutionError
from .swirl_client import SwirlClient, SwirlSearchError
from .workflow_commands import CommandExecutor
from .workflow_errors import WorkflowExecutionError
from .workflow_plane import PlaneImplementationMixin
from .workflow_search import search_workflow_context, summarize_retrieval
from .workflow_store import WorkflowStore
from .workflow_validation import AgentResultValidationMixin


class WorkflowEngine(PlaneImplementationMixin, AgentResultValidationMixin):
    def __init__(
        self,
        scenarios: ScenarioRegistry,
        store: WorkflowStore,
        agent_service: AgentService,
        *,
        command_executor: CommandExecutor | None = None,
        node_runtime: NodeRuntime | None = None,
        swirl_client: SwirlClient | None = None,
        audit_store: AuditStore | None = None,
        clock: Callable[[], datetime] | None = None,
        max_transitions_per_run: int = 100,
    ):
        self.scenarios = scenarios
        self.store = store
        self.agent_service = agent_service
        self._command_executor = command_executor or CommandExecutor()
        self.node_runtime = node_runtime or NodeRuntime(self._command_executor)
        self.swirl_client = swirl_client
        self.audit_store = audit_store
        self.clock = clock or (lambda: datetime.now(UTC))
        self.max_transitions_per_run = max_transitions_per_run
        self._creation_lock = Lock()

    @property
    def command_executor(self) -> CommandExecutor:
        return self._command_executor

    @command_executor.setter
    def command_executor(self, executor: CommandExecutor) -> None:
        self._command_executor = executor
        if hasattr(self, "node_runtime"):
            self.node_runtime.command_executor = executor

    def create(self, event: TriggerEvent) -> tuple[WorkflowInstance, bool]:
        scenario = self.scenarios.match(event)
        return self.create_for_scenario(scenario, event)

    def create_for_scenario(
        self,
        scenario: ScenarioManifest,
        event: TriggerEvent,
        *,
        workflow_id: str | None = None,
    ) -> tuple[WorkflowInstance, bool]:
        workflow_id = workflow_id or self.store.workflow_id(scenario, event)
        with self._creation_lock:
            existing = self.store.get(workflow_id)
            if existing is not None:
                return existing, False
            now = self.clock()
            scenario_snapshot_sha256 = self.store.save_scenario_snapshot(workflow_id, scenario)
            workflow = WorkflowInstance(
                id=workflow_id,
                scenario_id=scenario.id,
                scenario_version=scenario.version,
                scenario_snapshot_sha256=scenario_snapshot_sha256,
                trigger=event,
                status="CREATED",
                current_step=scenario.start_step,
                created_at=now,
                updated_at=now,
                deadline_at=now + timedelta(seconds=scenario.timeout_seconds),
            )
            self.store.save(workflow)
            self._audit(
                workflow,
                "workflow.created",
                {"scenario_id": scenario.id, "source": event.source, "event": event.event},
            )
        return workflow, True

    def start(self, event: TriggerEvent) -> WorkflowInstance:
        workflow, created = self.create(event)
        if not created:
            return workflow
        return self.advance(workflow)

    def advance_safely(
        self,
        workflow: WorkflowInstance,
        *,
        transition_budget: int | None = None,
    ) -> WorkflowInstance:
        try:
            return self.advance(workflow, transition_budget=transition_budget)
        except (OSError, RuntimeError, ValueError) as exc:
            workflow.status = "FAILED"
            workflow.outcome = None
            workflow.error = StepError(
                code="UNEXPECTED_WORKFLOW_ERROR",
                message=str(exc),
                retryable=False,
            )
            return self._save(workflow)

    def get(self, workflow_id: str) -> WorkflowInstance | None:
        return self.store.get(workflow_id)

    def _scenario_for(self, workflow: WorkflowInstance) -> ScenarioManifest:
        scenario = self.store.get_scenario_snapshot(workflow.id)
        if scenario is None:
            scenario = self.scenarios.get(workflow.scenario_id)
            if scenario.version != workflow.scenario_version:
                raise WorkflowExecutionError(
                    "legacy workflow has no snapshot and its scenario version is unavailable"
                )
            workflow.scenario_snapshot_sha256 = self.store.save_scenario_snapshot(
                workflow.id, scenario
            )
            return scenario
        if scenario.id != workflow.scenario_id or scenario.version != workflow.scenario_version:
            raise WorkflowExecutionError("workflow scenario snapshot identity does not match")
        digest = self.store.scenario_digest(scenario)
        if (
            workflow.scenario_snapshot_sha256 is not None
            and workflow.scenario_snapshot_sha256 != digest
        ):
            raise WorkflowExecutionError("workflow scenario snapshot digest does not match")
        workflow.scenario_snapshot_sha256 = digest
        return scenario

    def advance(
        self,
        workflow: WorkflowInstance,
        *,
        transition_budget: int | None = None,
    ) -> WorkflowInstance:
        scenario = self._scenario_for(workflow)
        if workflow.deadline_at is None:
            workflow.deadline_at = workflow.created_at + timedelta(seconds=scenario.timeout_seconds)
        if workflow.status in {"COMPLETED", "FAILED", "CANCELLED"}:
            return workflow
        if workflow.status == "WAITING" and workflow.pending_delay is None:
            return workflow
        terminal = self._external_terminal(workflow)
        if terminal is not None:
            return terminal
        if workflow.pending_delay is not None:
            if self.clock() < workflow.pending_delay.available_at:
                return workflow
            workflow = self._resume_delay(workflow, scenario)
            if workflow.current_step is None:
                workflow.status = "COMPLETED"
                workflow.outcome = self._terminal_outcome(workflow)
                return self._save(workflow)
            if transition_budget is not None:
                return workflow
        if self._deadline_exceeded(workflow):
            return self._save(workflow)
        workflow.status = "RUNNING"
        transition_limit = transition_budget or self.max_transitions_per_run
        for _ in range(transition_limit):
            terminal = self._external_terminal(workflow)
            if terminal is not None:
                return terminal
            if self._deadline_exceeded(workflow):
                return self._save(workflow)
            if workflow.current_step is None:
                workflow.status = "COMPLETED"
                workflow.outcome = self._terminal_outcome(workflow)
                return self._save(workflow)
            if workflow.pending_retry is not None:
                pending_retry = workflow.pending_retry
                if self.clock() < pending_retry.available_at:
                    return self._save(workflow)
                if workflow.current_step != pending_retry.step_id:
                    raise WorkflowExecutionError("pending retry does not match current step")
                step_id = pending_retry.step_id
                iteration = pending_retry.iteration
                attempt = pending_retry.next_attempt
                workflow.pending_retry = None
            else:
                step_id = workflow.current_step
                iteration = workflow.iterations.get(step_id, 0) + 1
                workflow.iterations[step_id] = iteration
                attempt = 1
            step = scenario.steps[step_id]
            try:
                node_execution = self.node_runtime.prepare(
                    workflow=workflow,
                    step_id=step_id,
                    iteration=iteration,
                    attempt=attempt,
                    step=step,
                )
                preparation_error = None
            except NodeRuntimeError as exc:
                node_execution = None
                preparation_error = exc
            if isinstance(step, ReviewScenarioStep) and node_execution is not None:
                review_ref = self._review_reference(workflow)
                execution = self._begin_execution(
                    workflow,
                    step_id=step_id,
                    iteration=iteration,
                    attempt=attempt,
                    inputs=node_execution.inputs,
                )
                self._change_execution_status(workflow, execution, "READY")
                self._change_execution_status(workflow, execution, "WAITING")
                workflow.status = "WAITING"
                workflow.pending_review = PendingReview(
                    step_id=step_id,
                    execution_id=execution.execution_id,
                    iteration=iteration,
                    provider=step.provider,
                    decision=step.decision,
                    inputs=node_execution.inputs,
                    **review_ref,
                )
                return self._save(workflow)
            if isinstance(step, DelayScenarioStep) and node_execution is not None:
                execution = self._begin_execution(
                    workflow,
                    step_id=step_id,
                    iteration=iteration,
                    attempt=attempt,
                    inputs=node_execution.inputs,
                )
                self._change_execution_status(workflow, execution, "READY")
                self._change_execution_status(workflow, execution, "WAITING")
                workflow.status = "WAITING"
                workflow.pending_delay = PendingDelay(
                    step_id=step_id,
                    execution_id=execution.execution_id,
                    iteration=iteration,
                    available_at=self.clock() + timedelta(seconds=step.seconds),
                    inputs=node_execution.inputs,
                )
                return self._save(workflow)

            execution = self._begin_execution(
                workflow,
                step_id=step_id,
                iteration=iteration,
                attempt=attempt,
                inputs=node_execution.inputs if node_execution is not None else {},
            )
            self._change_execution_status(workflow, execution, "READY")
            self._change_execution_status(workflow, execution, "RUNNING")
            if preparation_error is not None:
                result = self._technical_error(
                    workflow,
                    step_id,
                    iteration,
                    attempt,
                    "NODE_INPUT_ERROR",
                    str(preparation_error),
                )
            else:
                result = self._execute_once(node_execution)
            self._finish_execution(workflow, execution.execution_id, result)
            terminal = self._external_terminal(workflow)
            if terminal is not None:
                return terminal
            self._audit(
                workflow,
                "workflow.step.finished",
                {
                    "step_id": step_id,
                    "iteration": iteration,
                    "attempt": result.attempt,
                    "execution_status": result.execution_status,
                    "outcome": result.outcome,
                    "error_code": result.error.code if result.error else None,
                },
            )
            if self._deadline_exceeded(workflow):
                return self._save(workflow)
            if result.execution_status == "ERROR":
                if attempt < step.retry.max_attempts:
                    delay_seconds = step.retry.delay_for(attempt)
                    available_at = self.clock() + timedelta(seconds=delay_seconds)
                    if workflow.deadline_at is None or available_at < workflow.deadline_at:
                        workflow.pending_retry = PendingRetry(
                            step_id=step_id,
                            iteration=iteration,
                            next_attempt=attempt + 1,
                            available_at=available_at,
                        )
                        workflow.error = result.error
                        self._audit(
                            workflow,
                            "workflow.step.retry.scheduled",
                            {
                                "step_id": step_id,
                                "iteration": iteration,
                                "next_attempt": attempt + 1,
                                "available_at": available_at.isoformat(),
                            },
                        )
                        return self._save(workflow)
                workflow.status = "FAILED"
                workflow.outcome = None
                workflow.pending_retry = None
                workflow.error = (
                    result.error.model_copy(update={"retryable": False})
                    if result.error is not None
                    else None
                )
                return self._save(workflow)
            workflow.error = None
            self._transition(workflow, step, result.outcome)
            self._save(workflow)
            if transition_budget is not None:
                if workflow.current_step is None:
                    workflow.status = "COMPLETED"
                    workflow.outcome = self._terminal_outcome(workflow)
                    return self._save(workflow)
                return workflow
        if transition_budget is not None:
            return self._save(workflow)
        workflow.status = "FAILED"
        workflow.outcome = None
        workflow.error = StepError(
            code="TRANSITION_LIMIT",
            message="workflow exceeded the transition limit for one run",
            retryable=False,
        )
        return self._save(workflow)

    def review(
        self,
        workflow_id: str,
        decision: ReviewDecision,
        *,
        advance: bool = True,
        refreshed_trigger: TriggerEvent | None = None,
    ) -> WorkflowInstance:
        workflow = self.store.get(workflow_id)
        if workflow is None:
            raise WorkflowExecutionError(f"unknown workflow: {workflow_id}")
        if (
            decision.external_event_id is not None
            and decision.external_event_id in workflow.processed_event_ids
        ):
            return workflow
        if workflow.status != "WAITING" or workflow.pending_review is None:
            raise WorkflowExecutionError("workflow is not waiting for review")
        if refreshed_trigger is not None:
            current_ticket = workflow.trigger.data.get("ticket")
            refreshed_ticket = refreshed_trigger.data.get("ticket")
            current_ticket_id = (
                current_ticket.get("id") if isinstance(current_ticket, dict) else None
            )
            refreshed_ticket_id = (
                refreshed_ticket.get("id") if isinstance(refreshed_ticket, dict) else None
            )
            if (
                workflow.trigger.source != "plane"
                or refreshed_trigger.source != "plane"
                or not isinstance(current_ticket_id, str)
                or current_ticket_id != refreshed_ticket_id
            ):
                raise WorkflowExecutionError("review trigger does not match the Plane issue")
            workflow.trigger = refreshed_trigger
        scenario = self._scenario_for(workflow)
        pending = workflow.pending_review
        step = scenario.steps[pending.step_id]
        if not isinstance(step, ReviewScenarioStep):
            raise WorkflowExecutionError("pending workflow step is not a review")
        workflow.review_comments.extend(decision.comments)
        if decision.external_event_id is not None:
            workflow.processed_event_ids.append(decision.external_event_id)
        if not any(
            execution.execution_id == pending.execution_id for execution in workflow.executions
        ):
            workflow.executions.append(
                StepResult(
                    step_id=pending.step_id,
                    execution_id=pending.execution_id,
                    iteration=pending.iteration,
                    attempt=1,
                    execution_status="WAITING",
                    outcome=None,
                    status_history=[StepStatusChange(status="WAITING", occurred_at=self.clock())],
                )
            )
        self._finish_execution(
            workflow,
            pending.execution_id,
            StepResult(
                step_id=pending.step_id,
                execution_id=pending.execution_id,
                iteration=pending.iteration,
                attempt=1,
                execution_status="COMPLETED",
                outcome=decision.outcome,
                inputs=pending.inputs,
                data={
                    "summary": "Gitea review completed",
                    "comments": decision.comments,
                    "external_event_id": decision.external_event_id,
                    "external_url": decision.external_url,
                },
            ),
        )
        workflow.pending_review = None
        workflow.status = "RUNNING"
        self._transition(workflow, step, decision.outcome)
        self._audit(
            workflow,
            "workflow.review.completed",
            {
                "step_id": pending.step_id,
                "outcome": decision.outcome,
                "comment_count": len(decision.comments),
                "external_event_id": decision.external_event_id,
            },
        )
        self._save(workflow)
        return self.advance(workflow) if advance else workflow

    def _resume_delay(
        self,
        workflow: WorkflowInstance,
        scenario: ScenarioManifest,
    ) -> WorkflowInstance:
        pending = workflow.pending_delay
        if pending is None:
            raise WorkflowExecutionError("workflow has no pending delay")
        if workflow.current_step != pending.step_id:
            raise WorkflowExecutionError("pending delay does not match current step")
        step = scenario.steps[pending.step_id]
        if not isinstance(step, DelayScenarioStep):
            raise WorkflowExecutionError("pending workflow step is not a delay")
        self._finish_execution(
            workflow,
            pending.execution_id,
            StepResult(
                step_id=pending.step_id,
                execution_id=pending.execution_id,
                iteration=pending.iteration,
                attempt=1,
                execution_status="COMPLETED",
                outcome="SUCCESS",
                inputs=pending.inputs,
                data={
                    "summary": "Delay completed",
                    "available_at": pending.available_at.isoformat(),
                },
            ),
        )
        workflow.pending_delay = None
        workflow.status = "RUNNING"
        self._transition(workflow, step, "SUCCESS")
        self._audit(
            workflow,
            "workflow.delay.completed",
            {"step_id": pending.step_id, "available_at": pending.available_at.isoformat()},
        )
        return self._save(workflow)

    def cancel(self, workflow_id: str, *, reason: str) -> WorkflowInstance:
        workflow = self.store.get(workflow_id)
        if workflow is None:
            raise WorkflowExecutionError(f"unknown workflow: {workflow_id}")
        if workflow.status == "CANCELLED":
            return workflow
        if workflow.status in {"COMPLETED", "FAILED"}:
            raise WorkflowExecutionError(f"workflow cannot be cancelled from {workflow.status}")
        self.store.mark_cancel_requested(workflow_id)
        now = self.clock()
        workflow.status = "CANCELLED"
        workflow.outcome = None
        workflow.cancel_requested_at = now
        workflow.cancelled_at = now
        workflow.pending_review = None
        workflow.pending_delay = None
        workflow.pending_retry = None
        workflow.error = StepError(
            code="WORKFLOW_CANCELLED",
            message=reason,
            retryable=False,
        )
        self._cancel_active_execution(workflow, workflow.error)
        self._audit(
            workflow,
            "workflow.cancelled",
            {"previous_step": workflow.current_step, "reason": reason},
        )
        return self._save(workflow)

    def retry(self, workflow_id: str, *, reason: str) -> WorkflowInstance:
        workflow = self.store.get(workflow_id)
        if workflow is None:
            raise WorkflowExecutionError(f"unknown workflow: {workflow_id}")
        if workflow.status != "FAILED":
            raise WorkflowExecutionError(f"workflow cannot be retried from {workflow.status}")
        if workflow.current_step is None:
            raise WorkflowExecutionError("workflow has no step to retry")
        scenario = self._scenario_for(workflow)
        now = self.clock()
        workflow.status = "CREATED"
        workflow.outcome = None
        workflow.error = None
        workflow.pending_review = None
        workflow.pending_delay = None
        workflow.pending_retry = None
        workflow.deadline_at = now + timedelta(seconds=scenario.timeout_seconds)
        workflow.cancel_requested_at = None
        workflow.cancelled_at = None
        self.store.clear_cancel_requested(workflow_id)
        self._audit(
            workflow,
            "workflow.retry.requested",
            {"current_step": workflow.current_step, "reason": reason},
        )
        return self._save(workflow)

    def fail_processing(self, workflow_id: str, *, message: str) -> WorkflowInstance:
        workflow = self.store.get(workflow_id)
        if workflow is None:
            raise WorkflowExecutionError(f"unknown workflow: {workflow_id}")
        if workflow.status in {"COMPLETED", "FAILED", "CANCELLED"}:
            return workflow
        error = StepError(
            code="WORKFLOW_QUEUE_FAILED",
            message=message[:2000],
            retryable=True,
        )
        workflow.status = "FAILED"
        workflow.outcome = None
        workflow.pending_review = None
        workflow.pending_delay = None
        workflow.pending_retry = None
        workflow.error = error
        self._fail_active_execution(workflow, error)
        self._audit(
            workflow,
            "workflow.queue.failed",
            {"current_step": workflow.current_step, "message": message[:2000]},
        )
        return self._save(workflow)

    @staticmethod
    def _review_reference(workflow: WorkflowInstance) -> dict[str, Any]:
        for result in reversed(workflow.executions):
            pull = result.data.get("pull_request")
            if not isinstance(pull, dict):
                continue
            repository = pull.get("repository")
            index = pull.get("index")
            url = pull.get("url") or pull.get("html_url")
            return {
                "repository": str(repository)[:300] if repository else None,
                "pull_index": index if isinstance(index, int) and index > 0 else None,
                "url": str(url)[:4000] if url else None,
            }
        return {}

    @staticmethod
    def _terminal_outcome(workflow: WorkflowInstance) -> str:
        for execution in reversed(workflow.executions):
            if execution.execution_status == "COMPLETED" and execution.outcome is not None:
                return execution.outcome
        raise WorkflowExecutionError("completed workflow has no terminal business outcome")

    def _begin_execution(
        self,
        workflow: WorkflowInstance,
        *,
        step_id: str,
        iteration: int,
        attempt: int,
        inputs: dict[str, Any] | None = None,
    ) -> StepResult:
        now = self.clock()
        execution = StepResult(
            step_id=step_id,
            execution_id=CommandExecutor.execution_id(workflow.id, step_id, iteration, attempt),
            iteration=iteration,
            attempt=attempt,
            execution_status="PENDING",
            outcome=None,
            inputs=inputs or {},
            status_history=[StepStatusChange(status="PENDING", occurred_at=now)],
        )
        workflow.executions.append(execution)
        self._audit_execution_status(workflow, execution)
        self._save(workflow)
        return execution

    def _change_execution_status(
        self,
        workflow: WorkflowInstance,
        execution: StepResult,
        status: str,
    ) -> None:
        execution.execution_status = status
        execution.status_history.append(StepStatusChange(status=status, occurred_at=self.clock()))
        self._audit_execution_status(workflow, execution)
        self._save(workflow)

    def _finish_execution(
        self,
        workflow: WorkflowInstance,
        execution_id: str,
        result: StepResult,
    ) -> None:
        for index, execution in enumerate(workflow.executions):
            if execution.execution_id != execution_id:
                continue
            result.status_history = [
                *execution.status_history,
                StepStatusChange(status=result.execution_status, occurred_at=self.clock()),
            ]
            workflow.executions[index] = result
            self._audit_execution_status(workflow, result)
            self._save(workflow)
            return
        raise WorkflowExecutionError(f"step execution is missing: {execution_id}")

    def _audit_execution_status(
        self,
        workflow: WorkflowInstance,
        execution: StepResult,
    ) -> None:
        self._audit(
            workflow,
            "workflow.step.status.changed",
            {
                "step_id": execution.step_id,
                "execution_id": execution.execution_id,
                "iteration": execution.iteration,
                "attempt": execution.attempt,
                "execution_status": execution.execution_status,
            },
        )

    def _execute_once(self, execution: NodeExecutionInput) -> StepResult:
        try:
            return self.node_runtime.execute(
                execution,
                agent_executor=self._execute_agent,
            )
        except WorkflowExecutionError as exc:
            code = (
                "COMMAND_ERROR"
                if isinstance(execution.step, CommandScenarioStep)
                else "NODE_EXECUTION_ERROR"
            )
            return self._technical_error(
                execution.workflow,
                execution.step_id,
                execution.iteration,
                execution.attempt,
                code,
                str(exc),
                inputs=execution.inputs,
            )

    def _execute_agent(
        self,
        execution: NodeExecutionInput,
    ) -> StepResult:
        workflow = execution.workflow
        step_id = execution.step_id
        iteration = execution.iteration
        attempt = execution.attempt
        step = execution.step
        if not isinstance(step, AgentScenarioStep):
            raise WorkflowExecutionError("agent executor received a non-agent node")
        try:
            timeout_seconds = step.timeout_seconds
            if workflow.deadline_at is not None:
                remaining = math.ceil((workflow.deadline_at - self.clock()).total_seconds())
                timeout_seconds = max(1, min(timeout_seconds, remaining))
            request = AgentRunRequest(
                execution_id=CommandExecutor.execution_id(workflow.id, step_id, iteration, attempt),
                workflow_id=workflow.id,
                iteration=iteration,
                attempt=attempt,
                step=AgentStep(
                    id=step_id,
                    prompt=step.prompt,
                    plugins=step.plugins,
                    provider=step.provider,
                    model=step.model,
                    timeout_seconds=timeout_seconds,
                ),
                context=self._context(workflow, step, node_inputs=execution.inputs),
            )
            result = self.agent_service.run(request)
            return self._validate_agent_result(
                workflow,
                step_id,
                iteration,
                attempt,
                step,
                result,
            )
        except (
            CapabilityResolutionError,
            IdempotencyConflict,
            ImageBuildError,
            ImageResolutionError,
            PluginResolutionError,
            SandboxExecutionError,
            SkillResolutionError,
            SwirlSearchError,
        ) as exc:
            return self._technical_error(
                workflow, step_id, iteration, attempt, "AGENT_EXECUTION_ERROR", str(exc)
            )

    def _context(
        self,
        workflow: WorkflowInstance,
        step: AgentScenarioStep,
        *,
        node_inputs: dict[str, Any] | None = None,
    ) -> WorkflowContext:
        swirl_results: list[dict[str, Any]] = []
        retrieval_summary: dict[str, Any] = {}
        if step.context_search is not None:
            if self.swirl_client is None:
                raise SwirlSearchError("SWIRL context search is requested but not configured")
            policy = step.context_search
            query = policy.query
            if policy.query_field is not None:
                value: Any = workflow.trigger.data
                for part in policy.query_field.split("."):
                    if not isinstance(value, dict) or part not in value:
                        raise SwirlSearchError(
                            f"SWIRL query field is missing: {policy.query_field}"
                        )
                    value = value[part]
                if not isinstance(value, (str, int, float)):
                    raise SwirlSearchError(
                        f"SWIRL query field must be scalar: {policy.query_field}"
                    )
                query = str(value)
            results = search_workflow_context(
                self.swirl_client,
                query or "",
                providers=policy.providers,
                max_results=policy.max_results,
                fallback_on_empty=policy.fallback_on_empty,
                max_fallback_queries=policy.max_fallback_queries,
                expand_query=policy.expand_query,
                rank_fusion_k=policy.rank_fusion_k,
                focused_query_weight=policy.focused_query_weight,
                fetch_content=policy.fetch_content,
                max_content_documents=policy.max_content_documents,
                max_content_characters=policy.max_content_characters,
                min_content_documents=policy.min_content_documents,
                max_context_characters=policy.max_context_characters,
                max_chunk_characters=policy.max_chunk_characters,
                max_chunks_per_document=policy.max_chunks_per_document,
                max_context_results=policy.max_context_results,
                max_snippet_characters=policy.max_snippet_characters,
                min_chunk_relevance=policy.min_chunk_relevance,
            )
            retrieval_summary = summarize_retrieval(query or "", results)
            swirl_results = [item.model_dump(mode="json") for item in results]
        return WorkflowContext(
            trigger_data=workflow.trigger.data,
            node_inputs=node_inputs or {},
            scenario={
                "workflow_id": workflow.id,
                "scenario_id": workflow.scenario_id,
                "scenario_version": workflow.scenario_version,
                "current_step": workflow.current_step,
            },
            previous_steps=[
                PreviousStepResult(
                    step_id=result.step_id,
                    execution_status=result.execution_status,
                    outcome=result.outcome,
                    data=result.data,
                    artifacts=result.artifacts,
                )
                for result in workflow.executions
            ],
            review_comments=workflow.review_comments,
            retrieval_summary=retrieval_summary,
            swirl_results=swirl_results,
        )

    @staticmethod
    def _technical_error(
        workflow: WorkflowInstance,
        step_id: str,
        iteration: int,
        attempt: int,
        code: str,
        message: str,
        inputs: dict[str, Any] | None = None,
    ) -> StepResult:
        return StepResult(
            step_id=step_id,
            execution_id=CommandExecutor.execution_id(workflow.id, step_id, iteration, attempt),
            iteration=iteration,
            attempt=attempt,
            execution_status="ERROR",
            outcome=None,
            inputs=inputs or {},
            data={},
            artifacts=[],
            error=StepError(code=code, message=message, retryable=True),
        )

    @staticmethod
    def _transition(workflow: WorkflowInstance, step: Any, outcome: str | None) -> None:
        if outcome not in {"SUCCESS", "FAILURE"}:
            raise WorkflowExecutionError("completed step has no business outcome")
        workflow.current_step = step.transitions[outcome]

    def _external_terminal(self, workflow: WorkflowInstance) -> WorkflowInstance | None:
        if not self.store.is_cancel_requested(workflow.id):
            return None
        latest = self.store.get(workflow.id)
        if latest is not None and latest.status == "CANCELLED":
            return latest
        now = self.clock()
        workflow.status = "CANCELLED"
        workflow.cancel_requested_at = workflow.cancel_requested_at or now
        workflow.cancelled_at = now
        workflow.pending_review = None
        workflow.pending_delay = None
        workflow.pending_retry = None
        workflow.error = StepError(
            code="WORKFLOW_CANCELLED",
            message="Cancellation was requested",
            retryable=False,
        )
        self._cancel_active_execution(workflow, workflow.error)
        return self._save(workflow)

    def _cancel_active_execution(
        self,
        workflow: WorkflowInstance,
        error: StepError,
    ) -> None:
        for execution in reversed(workflow.executions):
            if execution.execution_status not in {"PENDING", "READY", "RUNNING", "WAITING"}:
                continue
            execution.execution_status = "CANCELLED"
            execution.outcome = None
            execution.error = error
            execution.status_history.append(
                StepStatusChange(status="CANCELLED", occurred_at=self.clock())
            )
            self._audit_execution_status(workflow, execution)
            return

    def _fail_active_execution(self, workflow: WorkflowInstance, error: StepError) -> None:
        for execution in reversed(workflow.executions):
            if execution.execution_status not in {"PENDING", "READY", "RUNNING", "WAITING"}:
                continue
            execution.execution_status = "ERROR"
            execution.outcome = None
            execution.error = error
            execution.status_history.append(
                StepStatusChange(status="ERROR", occurred_at=self.clock())
            )
            self._audit_execution_status(workflow, execution)
            return

    def _deadline_exceeded(self, workflow: WorkflowInstance) -> bool:
        if workflow.deadline_at is None or self.clock() < workflow.deadline_at:
            return False
        workflow.status = "FAILED"
        workflow.outcome = None
        workflow.pending_retry = None
        workflow.pending_delay = None
        workflow.error = StepError(
            code="WORKFLOW_DEADLINE_EXCEEDED",
            message="workflow exceeded its overall deadline",
            retryable=True,
        )
        self._audit(
            workflow,
            "workflow.deadline.exceeded",
            {"deadline_at": workflow.deadline_at.isoformat()},
        )
        return True

    def _save(self, workflow: WorkflowInstance) -> WorkflowInstance:
        if self.store.is_cancel_requested(workflow.id) and workflow.status != "CANCELLED":
            latest = self.store.get(workflow.id)
            if latest is not None and latest.status == "CANCELLED":
                return latest
            now = self.clock()
            workflow.status = "CANCELLED"
            workflow.outcome = None
            workflow.cancel_requested_at = workflow.cancel_requested_at or now
            workflow.cancelled_at = now
            workflow.pending_review = None
            workflow.pending_delay = None
            workflow.pending_retry = None
            workflow.error = StepError(
                code="WORKFLOW_CANCELLED",
                message="Cancellation was requested",
                retryable=False,
            )
            self._cancel_active_execution(workflow, workflow.error)
        workflow.updated_at = self.clock()
        self.store.save(workflow)
        self._audit(
            workflow,
            "workflow.state.saved",
            {
                "status": workflow.status,
                "outcome": workflow.outcome,
                "current_step": workflow.current_step,
                "execution_count": len(workflow.executions),
                "error_code": workflow.error.code if workflow.error else None,
            },
        )
        return workflow

    def _audit(
        self,
        workflow: WorkflowInstance,
        action: str,
        details: dict[str, Any],
    ) -> None:
        if self.audit_store is None:
            return
        self.audit_store.record(
            actor="orchestrator",
            action=action,
            resource_type="workflow",
            resource_id=workflow.id,
            details=details,
        )
