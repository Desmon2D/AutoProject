from __future__ import annotations

import math
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any

from .audit_store import AuditStore
from .capability_registry import CapabilityResolutionError
from .gitea_client import GiteaClient, GiteaClientError
from .image_builder import ImageBuildError
from .image_registry import ImageResolutionError
from .job_store import IdempotencyConflict
from .models import (
    AgentRunRequest,
    AgentScenarioStep,
    AgentStep,
    ArtifactRef,
    CommandScenarioStep,
    PendingRetry,
    PendingReview,
    PreviousStepResult,
    ReviewDecision,
    ReviewScenarioStep,
    StepError,
    StepResult,
    StepStatusChange,
    TriggerEvent,
    WorkflowContext,
    WorkflowInstance,
)
from .plane_client import PlaneClient, PlaneClientError
from .plugin_registry import PluginResolutionError
from .sandbox_manager import SandboxExecutionError
from .scenario_registry import ScenarioRegistry
from .service import AgentService
from .skill_registry import SkillResolutionError
from .swirl_client import SwirlClient, SwirlSearchError
from .workflow_store import WorkflowStore


class WorkflowExecutionError(RuntimeError):
    pass


class CommandExecutor:
    def __init__(
        self,
        gitea_client: GiteaClient | None = None,
        plane_client: PlaneClient | None = None,
    ):
        self.gitea_client = gitea_client
        self.plane_client = plane_client

    def execute(
        self,
        *,
        workflow: WorkflowInstance,
        step_id: str,
        iteration: int,
        attempt: int,
        step: CommandScenarioStep,
    ) -> StepResult:
        if step.command not in {
            "complete",
            "fail",
            "store_failure_report",
            "allow_test_rewrite",
            "classify_test_run",
            "create_final_pull_request",
            "sync_plane_issue",
        }:
            raise WorkflowExecutionError(f"command is not allowlisted: {step.command}")
        artifacts: list[ArtifactRef] = []
        if step.command == "sync_plane_issue":
            if self.plane_client is None:
                plane_sync = {"configured": False}
            else:
                recommendation = step.parameters.get("recommendation")
                if not isinstance(recommendation, str):
                    raise WorkflowExecutionError("sync_plane_issue requires recommendation")
                ticket = workflow.trigger.data.get("ticket")
                project = workflow.trigger.data.get("project")
                issue_id = ticket.get("id") if isinstance(ticket, dict) else None
                project_id = project.get("id") if isinstance(project, dict) else None
                if not isinstance(project_id, str) and isinstance(project, dict):
                    references = project.get("references")
                    project_id = next(
                        (item for item in references if isinstance(item, str)),
                        None,
                    ) if isinstance(references, list) else None
                if not isinstance(issue_id, str) or not isinstance(project_id, str):
                    raise WorkflowExecutionError("Plane issue and project ids are required")
                details: dict[str, Any] = {}
                for key in ("implementation_change", "test_report", "pull_request"):
                    value = next(
                        (
                            execution.data.get(key)
                            for execution in reversed(workflow.executions)
                            if isinstance(execution.data.get(key), dict)
                        ),
                        None,
                    )
                    if isinstance(value, dict):
                        details[key] = value
                try:
                    plane_sync = self.plane_client.record_result(
                        project_id=project_id,
                        issue_id=issue_id,
                        workflow_id=workflow.id,
                        recommendation=recommendation,
                        summary=str(step.parameters.get("summary", recommendation)),
                        details=details,
                    )
                except PlaneClientError as exc:
                    raise WorkflowExecutionError(str(exc)) from exc
            outcome = "SUCCESS"
            data = {"plane_sync": plane_sync}
            default_summary = "Plane issue synchronized"
        elif step.command == "create_final_pull_request":
            if self.gitea_client is None:
                raise WorkflowExecutionError("Gitea client is not configured")
            authored = next(
                (
                    result.data.get("test_change")
                    for result in reversed(workflow.executions)
                    if result.step_id == step.parameters.get("author_step", "write-tests")
                    and result.execution_status == "COMPLETED"
                    and result.outcome == "SUCCESS"
                    and isinstance(result.data.get("test_change"), dict)
                ),
                None,
            )
            executed = next(
                (
                    result.data.get("test_report")
                    for result in reversed(workflow.executions)
                    if result.step_id == step.parameters.get("executor_step", "execute-tests")
                    and result.execution_status == "COMPLETED"
                    and result.outcome == "SUCCESS"
                    and isinstance(result.data.get("test_report"), dict)
                ),
                None,
            )
            if not isinstance(authored, dict) or not isinstance(executed, dict):
                raise WorkflowExecutionError("validated test change and execution report are required")
            if executed.get("verdict") != "PASSED" or any(
                executed.get(field) != authored.get(field)
                for field in ("repository", "branch", "commit")
            ):
                raise WorkflowExecutionError("only the exact passing test commit may be proposed")
            ticket = workflow.trigger.data.get("ticket")
            title = ticket.get("summary") if isinstance(ticket, dict) else None
            try:
                pull = self.gitea_client.create_final_pull_request(
                    repository=authored["repository"],
                    head=authored["branch"],
                    commit=authored["commit"],
                    workflow_id=workflow.id,
                    title=str(title or f"Validated changes for {workflow.id}"),
                )
            except (GiteaClientError, KeyError) as exc:
                raise WorkflowExecutionError(str(exc)) from exc
            outcome = "SUCCESS"
            data = {"pull_request": pull, "test_report": executed}
            default_summary = "Final pull request created after successful tests"
            artifacts = [
                ArtifactRef(
                    type="pull_request",
                    uri=pull["url"],
                    summary="Final validated pull request",
                )
            ]
        elif step.command == "store_failure_report":
            failed = next(
                (result for result in reversed(workflow.executions) if result.outcome == "FAILURE"),
                None,
            )
            outcome = "SUCCESS" if failed is not None else "FAILURE"
            data = {
                "failed_step": failed.step_id if failed else None,
                "failure": failed.data if failed else {},
                "artifacts": [artifact.model_dump() for artifact in failed.artifacts]
                if failed
                else [],
            }
            default_summary = (
                "Failure report stored in workflow state"
                if failed
                else "No failed step result is available"
            )
        elif step.command == "allow_test_rewrite":
            author_step = step.parameters.get("author_step")
            max_iterations = step.parameters.get("max_iterations")
            if not isinstance(author_step, str) or not author_step:
                raise WorkflowExecutionError("allow_test_rewrite requires author_step")
            if type(max_iterations) is not int or not 1 <= max_iterations <= 10:
                raise WorkflowExecutionError(
                    "allow_test_rewrite max_iterations must be an integer from 1 to 10"
                )
            authored = sum(
                result.step_id == author_step
                and result.execution_status == "COMPLETED"
                and result.outcome == "SUCCESS"
                for result in workflow.executions
            )
            outcome = "SUCCESS" if authored < max_iterations else "FAILURE"
            data = {
                "author_step": author_step,
                "completed_iterations": authored,
                "max_iterations": max_iterations,
            }
            default_summary = (
                "Test author may repair invalid test code"
                if outcome == "SUCCESS"
                else "Test rewrite limit reached"
            )
        elif step.command == "classify_test_run":
            executor_step = step.parameters.get("executor_step")
            if not isinstance(executor_step, str) or not executor_step:
                raise WorkflowExecutionError("classify_test_run requires executor_step")
            executed = next(
                (
                    result
                    for result in reversed(workflow.executions)
                    if result.step_id == executor_step
                    and result.execution_status == "COMPLETED"
                    and result.outcome == "SUCCESS"
                ),
                None,
            )
            report = executed.data.get("test_report") if executed else None
            if not isinstance(report, dict):
                raise WorkflowExecutionError("no successful test execution report is available")
            verdict = report.get("verdict")
            if verdict not in {"PASSED", "PRODUCT_FAILURE"}:
                raise WorkflowExecutionError("test execution report has no final verdict")
            outcome = "SUCCESS" if verdict == "PASSED" else "FAILURE"
            data = {"test_report": report}
            default_summary = (
                "All authored tests passed"
                if outcome == "SUCCESS"
                else "Authored tests found a product defect"
            )
        else:
            outcome = "SUCCESS" if step.command == "complete" else "FAILURE"
            data = step.parameters.get("data", {})
            default_summary = f"Command {step.command} completed"
        summary = str(step.parameters.get("summary", default_summary))
        if not isinstance(data, dict):
            raise WorkflowExecutionError("command parameters.data must be an object")
        return StepResult(
            step_id=step_id,
            execution_id=self.execution_id(workflow.id, step_id, iteration, attempt),
            iteration=iteration,
            attempt=attempt,
            execution_status="COMPLETED",
            outcome=outcome,
            data={"summary": summary, **data},
            artifacts=artifacts,
        )

    @staticmethod
    def execution_id(workflow_id: str, step_id: str, iteration: int, attempt: int) -> str:
        return f"{workflow_id}-{step_id}-{iteration}-{attempt}"


class WorkflowEngine:
    def __init__(
        self,
        scenarios: ScenarioRegistry,
        store: WorkflowStore,
        agent_service: AgentService,
        *,
        command_executor: CommandExecutor | None = None,
        swirl_client: SwirlClient | None = None,
        audit_store: AuditStore | None = None,
        clock: Callable[[], datetime] | None = None,
        max_transitions_per_run: int = 100,
    ):
        self.scenarios = scenarios
        self.store = store
        self.agent_service = agent_service
        self.command_executor = command_executor or CommandExecutor()
        self.swirl_client = swirl_client
        self.audit_store = audit_store
        self.clock = clock or (lambda: datetime.now(UTC))
        self.max_transitions_per_run = max_transitions_per_run
        self._creation_lock = Lock()

    def create(self, event: TriggerEvent) -> tuple[WorkflowInstance, bool]:
        scenario = self.scenarios.match(event)
        workflow_id = self.store.workflow_id(scenario, event)
        with self._creation_lock:
            existing = self.store.get(workflow_id)
            if existing is not None:
                return existing, False
            now = self.clock()
            workflow = WorkflowInstance(
                id=workflow_id,
                scenario_id=scenario.id,
                scenario_version=scenario.version,
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

    def attach_plane_implementation(self, event: TriggerEvent) -> TriggerEvent:
        """Attach the exact implementation produced earlier for the same Plane issue."""
        if event.source != "plane" or event.event != "issue.testing":
            return event
        ticket = event.data.get("ticket")
        repository_data = event.data.get("repository")
        ticket_id = ticket.get("id") if isinstance(ticket, dict) else None
        repository = (
            repository_data.get("full_name") if isinstance(repository_data, dict) else None
        )
        supplied_ref = (
            repository_data.get("implementation_ref")
            if isinstance(repository_data, dict)
            else None
        )
        if not isinstance(ticket_id, str) or not isinstance(repository, str):
            raise WorkflowExecutionError("testing event has no Plane issue or repository")

        enriched_event = event
        plane_client = self.command_executor.plane_client
        if supplied_ref is None and plane_client is not None:
            project = event.data.get("project")
            project_id = project.get("id") if isinstance(project, dict) else None
            if not isinstance(project_id, str) and isinstance(project, dict):
                references = project.get("references")
                project_id = (
                    next((item for item in references if isinstance(item, str)), None)
                    if isinstance(references, list)
                    else None
                )
            if isinstance(project_id, str):
                try:
                    source = plane_client.get_implementation_source(
                        project_id=project_id,
                        issue_id=ticket_id,
                    )
                except PlaneClientError as exc:
                    raise WorkflowExecutionError(str(exc)) from exc
                if source is not None:
                    enriched_event = event.model_copy(deep=True)
                    enriched_event.data["repository"].update(source)
                    repository_data = enriched_event.data["repository"]
                    supplied_ref = source["implementation_ref"]

        for workflow in self.store.list():
            source_ticket = workflow.trigger.data.get("ticket")
            source_repository = workflow.trigger.data.get("repository")
            if (
                workflow.scenario_id != "implement-ticket"
                or workflow.status != "COMPLETED"
                or workflow.outcome != "SUCCESS"
                or not isinstance(source_ticket, dict)
                or source_ticket.get("id") != ticket_id
                or not isinstance(source_repository, dict)
                or source_repository.get("full_name") != repository
            ):
                continue
            change = next(
                (
                    execution.data.get("implementation_change")
                    for execution in reversed(workflow.executions)
                    if execution.execution_status == "COMPLETED"
                    and execution.outcome == "SUCCESS"
                    and isinstance(execution.data.get("implementation_change"), dict)
                ),
                None,
            )
            if not isinstance(change, dict):
                continue
            branch = change.get("branch")
            commit = change.get("commit")
            if (
                change.get("repository") != repository
                or not isinstance(branch, str)
                or not branch
                or not isinstance(commit, str)
                or re.fullmatch(r"[0-9a-fA-F]{40}", commit) is None
            ):
                continue
            if supplied_ref is not None and supplied_ref != branch:
                raise WorkflowExecutionError(
                    "Plane implementation ref does not match the completed implementation workflow"
                )
            enriched = enriched_event.model_copy(deep=True)
            enriched_repository = enriched.data["repository"]
            enriched_repository["implementation_ref"] = branch
            enriched_repository["implementation_commit"] = commit.lower()
            enriched_repository["implementation_workflow_id"] = workflow.id
            return enriched

        if supplied_ref is not None:
            return enriched_event
        raise WorkflowExecutionError(
            "no completed implementation workflow was found for this Plane issue"
        )

    def start(self, event: TriggerEvent) -> WorkflowInstance:
        workflow, created = self.create(event)
        if not created:
            return workflow
        return self.advance(workflow)

    def advance_safely(self, workflow: WorkflowInstance) -> WorkflowInstance:
        try:
            return self.advance(workflow)
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

    def advance(self, workflow: WorkflowInstance) -> WorkflowInstance:
        scenario = self.scenarios.get(workflow.scenario_id)
        if scenario.version != workflow.scenario_version:
            raise WorkflowExecutionError("scenario version changed during workflow execution")
        if workflow.deadline_at is None:
            workflow.deadline_at = workflow.created_at + timedelta(seconds=scenario.timeout_seconds)
        if workflow.status in {"WAITING", "COMPLETED", "FAILED", "CANCELLED"}:
            return workflow
        terminal = self._external_terminal(workflow)
        if terminal is not None:
            return terminal
        if self._deadline_exceeded(workflow):
            return self._save(workflow)
        workflow.status = "RUNNING"
        for _ in range(self.max_transitions_per_run):
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
            if isinstance(step, ReviewScenarioStep):
                review_ref = self._review_reference(workflow)
                execution = self._begin_execution(
                    workflow,
                    step_id=step_id,
                    iteration=iteration,
                    attempt=attempt,
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
                    **review_ref,
                )
                return self._save(workflow)

            execution = self._begin_execution(
                workflow,
                step_id=step_id,
                iteration=iteration,
                attempt=attempt,
            )
            self._change_execution_status(workflow, execution, "READY")
            self._change_execution_status(workflow, execution, "RUNNING")
            result = self._execute_once(workflow, step_id, iteration, attempt, step)
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
        scenario = self.scenarios.get(workflow.scenario_id)
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
        scenario = self.scenarios.get(workflow.scenario_id)
        now = self.clock()
        workflow.status = "CREATED"
        workflow.outcome = None
        workflow.error = None
        workflow.pending_review = None
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
    ) -> StepResult:
        now = self.clock()
        execution = StepResult(
            step_id=step_id,
            execution_id=CommandExecutor.execution_id(workflow.id, step_id, iteration, attempt),
            iteration=iteration,
            attempt=attempt,
            execution_status="PENDING",
            outcome=None,
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

    def _execute_once(
        self,
        workflow: WorkflowInstance,
        step_id: str,
        iteration: int,
        attempt: int,
        step: Any,
    ) -> StepResult:
        if isinstance(step, CommandScenarioStep):
            try:
                return self.command_executor.execute(
                    workflow=workflow,
                    step_id=step_id,
                    iteration=iteration,
                    attempt=attempt,
                    step=step,
                )
            except WorkflowExecutionError as exc:
                return self._technical_error(
                    workflow, step_id, iteration, attempt, "COMMAND_ERROR", str(exc)
                )
        if isinstance(step, AgentScenarioStep):
            return self._execute_agent(workflow, step_id, iteration, attempt, step)
        raise WorkflowExecutionError("unsupported workflow step")

    def _execute_agent(
        self,
        workflow: WorkflowInstance,
        step_id: str,
        iteration: int,
        attempt: int,
        step: AgentScenarioStep,
    ) -> StepResult:
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
                context=self._context(workflow, step),
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

    def _validate_agent_result(
        self,
        workflow: WorkflowInstance,
        step_id: str,
        iteration: int,
        attempt: int,
        step: AgentScenarioStep,
        result: StepResult,
    ) -> StepResult:
        if result.execution_status != "COMPLETED":
            return result
        if step.result_contract == "test_execution":
            return self._validate_test_execution_result(
                workflow, step_id, iteration, attempt, result
            )
        if result.outcome != "SUCCESS":
            return result
        if step.result_contract == "test_change":
            return self._validate_test_change_result(
                workflow, step_id, iteration, attempt, result
            )
        if step.result_contract == "implementation_change":
            return self._validate_implementation_change_result(
                workflow, step_id, iteration, attempt, result
            )
        next_step_id = step.transitions.get("SUCCESS")
        if next_step_id is None:
            return result
        scenario = self.scenarios.get(workflow.scenario_id)
        if step.result_contract != "pull_request" and not isinstance(
            scenario.steps[next_step_id], ReviewScenarioStep
        ):
            return result

        pull = result.data.get("pull_request")
        if not isinstance(pull, dict):
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_RESULT_PULL_REQUEST_INVALID",
                "successful implementation must return data.pull_request",
            )
        repository = pull.get("repository")
        index = pull.get("index")
        url = pull.get("url") or pull.get("html_url")
        if (
            not isinstance(repository, str)
            or not repository.strip()
            or not isinstance(index, int)
            or index < 1
            or not isinstance(url, str)
            or not url.startswith(("http://", "https://"))
        ):
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_RESULT_PULL_REQUEST_INVALID",
                "data.pull_request must contain repository, positive index, and HTTP URL",
            )
        if not any(
            artifact.type == "pull_request" and artifact.uri == url
            for artifact in result.artifacts
        ):
            result = result.model_copy(
                update={
                    "artifacts": [
                        *result.artifacts,
                        ArtifactRef(
                            type="pull_request",
                            uri=url,
                            summary="Pull request returned by the implementation agent",
                        ),
                    ]
                }
            )

        expected = self._review_reference(workflow)
        expected_repository = expected.get("repository")
        expected_index = expected.get("pull_index")
        if expected_repository is not None and expected_repository != repository:
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_PULL_REQUEST_CHANGED",
                "implementation iteration changed the reviewed repository",
            )
        if expected_index is not None and expected_index != index:
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_PULL_REQUEST_CHANGED",
                "implementation iteration created a different pull request",
            )
        return result

    def _validate_implementation_change_result(
        self,
        workflow: WorkflowInstance,
        step_id: str,
        iteration: int,
        attempt: int,
        result: StepResult,
    ) -> StepResult:
        change = result.data.get("implementation_change")
        repository_data = workflow.trigger.data.get("repository")
        expected_repository = (
            repository_data.get("full_name") if isinstance(repository_data, dict) else None
        )
        if not isinstance(change, dict):
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_IMPLEMENTATION_CHANGE_INVALID",
                "successful implementation must return data.implementation_change",
            )
        if (
            change.get("repository") != expected_repository
            or change.get("branch") != f"automation/{workflow.id}"
            or not isinstance(change.get("base_ref"), str)
            or not change["base_ref"].strip()
            or not isinstance(change.get("commit"), str)
            or re.fullmatch(r"[0-9a-fA-F]{40}", change["commit"]) is None
        ):
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_IMPLEMENTATION_CHANGE_INVALID",
                "implementation change must identify repository, base ref, stable branch, and commit",
            )
        gitea_client = self.command_executor.gitea_client
        if gitea_client is not None:
            try:
                gitea_client.verify_branch(
                    repository=change["repository"],
                    branch=change["branch"],
                    commit=change["commit"],
                )
            except GiteaClientError as exc:
                return self._technical_error(
                    workflow,
                    step_id,
                    iteration,
                    attempt,
                    "AGENT_IMPLEMENTATION_BRANCH_UNAVAILABLE",
                    str(exc),
                )
        return result

    def _validate_test_change_result(
        self,
        workflow: WorkflowInstance,
        step_id: str,
        iteration: int,
        attempt: int,
        result: StepResult,
    ) -> StepResult:
        change = result.data.get("test_change")
        if not isinstance(change, dict):
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_TEST_CHANGE_INVALID",
                "successful test authoring must return data.test_change",
            )
        repository = change.get("repository")
        base_ref = change.get("base_ref")
        branch = change.get("branch")
        commit = change.get("commit")
        repository_data = workflow.trigger.data.get("repository")
        expected_repository = (
            repository_data.get("full_name") if isinstance(repository_data, dict) else None
        )
        expected_base_ref = (
            repository_data.get("implementation_ref")
            if isinstance(repository_data, dict)
            else None
        )
        expected_branch = f"automation/{workflow.id}"
        if (
            not isinstance(repository, str)
            or repository != expected_repository
            or not isinstance(base_ref, str)
            or base_ref != expected_base_ref
            or branch != expected_branch
            or not isinstance(commit, str)
            or re.fullmatch(r"[0-9a-fA-F]{40}", commit) is None
        ):
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_TEST_CHANGE_INVALID",
                "data.test_change must identify the mapped repository, implementation ref, stable branch, and commit",
            )
        gitea_client = self.command_executor.gitea_client
        if gitea_client is not None:
            try:
                gitea_client.verify_branch(
                    repository=repository,
                    branch=branch,
                    commit=commit,
                )
            except GiteaClientError as exc:
                return self._technical_error(
                    workflow,
                    step_id,
                    iteration,
                    attempt,
                    "AGENT_TEST_BRANCH_UNAVAILABLE",
                    str(exc),
                )
        return result

    def _validate_test_execution_result(
        self,
        workflow: WorkflowInstance,
        step_id: str,
        iteration: int,
        attempt: int,
        result: StepResult,
    ) -> StepResult:
        report = result.data.get("test_report")
        authored = next(
            (
                execution.data.get("test_change")
                for execution in reversed(workflow.executions)
                if execution.step_id == "write-tests"
                and execution.execution_status == "COMPLETED"
                and execution.outcome == "SUCCESS"
                and isinstance(execution.data.get("test_change"), dict)
            ),
            None,
        )
        if not isinstance(report, dict) or not isinstance(authored, dict):
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_TEST_REPORT_INVALID",
                "test executor must return data.test_report for the authored test change",
            )
        verdict = report.get("verdict")
        command = report.get("command")
        exit_code = report.get("exit_code")
        passed = report.get("passed")
        failed = report.get("failed")
        summary = report.get("summary")
        same_revision = all(
            report.get(field) == authored.get(field)
            for field in ("repository", "branch", "commit")
        )
        if (
            verdict not in {"PASSED", "PRODUCT_FAILURE", "TEST_CODE_ERROR"}
            or not isinstance(command, str)
            or not command.strip()
            or type(exit_code) is not int
            or type(passed) is not int
            or passed < 0
            or type(failed) is not int
            or failed < 0
            or not isinstance(summary, str)
            or not summary.strip()
            or not same_revision
        ):
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_TEST_REPORT_INVALID",
                "data.test_report has invalid fields or does not match the authored commit",
            )
        valid_outcome = (
            verdict == "PASSED"
            and result.outcome == "SUCCESS"
            and exit_code == 0
            and passed > 0
            and failed == 0
        ) or (
            verdict == "PRODUCT_FAILURE"
            and result.outcome == "SUCCESS"
            and exit_code != 0
            and failed > 0
        ) or (
            verdict == "TEST_CODE_ERROR"
            and result.outcome == "FAILURE"
        )
        if not valid_outcome:
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_TEST_VERDICT_INVALID",
                "test verdict, outcome, exit code, and counters are inconsistent",
            )
        return result

    def _context(self, workflow: WorkflowInstance, step: AgentScenarioStep) -> WorkflowContext:
        swirl_results: list[dict[str, Any]] = []
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
            response = self.swirl_client.search(
                query or "",
                providers=policy.providers,
                max_results=policy.max_results,
            )
            swirl_results = [item.model_dump(mode="json") for item in response.results]
        return WorkflowContext(
            trigger_data=workflow.trigger.data,
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
    ) -> StepResult:
        return StepResult(
            step_id=step_id,
            execution_id=CommandExecutor.execution_id(workflow.id, step_id, iteration, attempt),
            iteration=iteration,
            attempt=attempt,
            execution_status="ERROR",
            outcome=None,
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
