import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from automation_orchestrator.context_builder import ContextBuilder
from automation_orchestrator.models import (
    AgentScenarioStep,
    ArtifactRef,
    CommandScenarioStep,
    ReviewDecision,
    StepResult,
    SwirlSearchResponse,
    SwirlSearchResult,
    TriggerEvent,
    WorkflowInstance,
)
from automation_orchestrator.sandbox_manager import SandboxManager
from automation_orchestrator.scenario_registry import ScenarioRegistry
from automation_orchestrator.service import AgentService
from automation_orchestrator.swirl_client import SwirlSearchError
from automation_orchestrator.workflow_engine import (
    CommandExecutor,
    WorkflowEngine,
    WorkflowExecutionError,
)
from automation_orchestrator.workflow_store import WorkflowStore


def build_engine(tmp_path: Path, image_resolver, scenario: dict) -> WorkflowEngine:
    scenario_root = tmp_path / "scenarios"
    scenario_root.mkdir()
    (scenario_root / "scenario.json").write_text(json.dumps(scenario), encoding="utf-8")
    agent_service = AgentService(
        ContextBuilder(),
        image_resolver,
        SandboxManager(tmp_path / "jobs"),
    )
    return WorkflowEngine(
        ScenarioRegistry(scenario_root),
        WorkflowStore(tmp_path / "workflows"),
        agent_service,
    )


def review_scenario() -> dict:
    return {
        "id": "review-flow",
        "trigger": {"source": "manual", "event": "review"},
        "start_step": "prepare",
        "steps": {
            "prepare": {
                "type": "command",
                "command": "complete",
                "parameters": {"summary": "prepared", "data": {"ticket": "A-1"}},
                "transitions": {"SUCCESS": "review", "FAILURE": None},
            },
            "review": {
                "type": "review",
                "provider": "gitea",
                "transitions": {"SUCCESS": "finish", "FAILURE": "finish"},
            },
            "finish": {
                "type": "command",
                "command": "complete",
                "transitions": {"SUCCESS": None, "FAILURE": None},
            },
        },
    }


def test_workflow_waits_for_review_and_resumes_idempotently(tmp_path: Path, image_resolver):
    engine = build_engine(tmp_path, image_resolver, review_scenario())
    event = TriggerEvent(source="manual", event="review", event_id="event-1", data={})

    waiting = engine.start(event)
    duplicate = engine.start(event)

    assert waiting.status == "WAITING"
    assert waiting.current_step == "review"
    assert waiting.pending_review is not None
    assert duplicate.id == waiting.id
    assert len(duplicate.executions) == 2
    assert duplicate.executions[-1].execution_status == "WAITING"
    assert [change.status for change in duplicate.executions[-1].status_history] == [
        "PENDING",
        "READY",
        "WAITING",
    ]

    completed = engine.review(
        waiting.id,
        ReviewDecision(outcome="SUCCESS", comments=["Approved"]),
    )

    assert completed.status == "COMPLETED"
    assert completed.outcome == "SUCCESS"
    assert completed.current_step is None
    assert [result.step_id for result in completed.executions] == [
        "prepare",
        "review",
        "finish",
    ]
    assert completed.review_comments == ["Approved"]
    assert [change.status for change in completed.executions[1].status_history] == [
        "PENDING",
        "READY",
        "WAITING",
        "COMPLETED",
    ]


def test_workflow_records_terminal_business_failure(tmp_path: Path, image_resolver):
    scenario = {
        "id": "business-failure",
        "trigger": {"source": "manual", "event": "failure"},
        "start_step": "stop",
        "steps": {
            "stop": {
                "type": "command",
                "command": "fail",
                "parameters": {"summary": "Manual action is required"},
                "transitions": {"SUCCESS": None, "FAILURE": None},
            }
        },
    }
    engine = build_engine(tmp_path, image_resolver, scenario)

    completed = engine.start(
        TriggerEvent(source="manual", event="failure", event_id="failure-1")
    )

    assert completed.status == "COMPLETED"
    assert completed.outcome == "FAILURE"
    assert completed.error is None


def test_implementation_failure_does_not_use_approval_terminal():
    registry = ScenarioRegistry(Path(__file__).parents[1] / "scenarios")
    scenario = registry.get("implement-ticket")

    report_step = scenario.steps["implementation-failed"]
    failure_step = scenario.steps["finish-failed"]

    assert report_step.transitions == {
        "SUCCESS": "sync-implementation-failed",
        "FAILURE": "sync-implementation-failed",
    }
    assert isinstance(failure_step, CommandScenarioStep)
    assert failure_step.command == "fail"
    assert "testable branch" in failure_step.parameters["summary"]


def test_testing_scenario_uses_separate_author_and_executor_agents():
    registry = ScenarioRegistry(Path(__file__).parents[1] / "scenarios")
    scenario = registry.get("test-ticket")

    author = scenario.steps["write-tests"]
    executor = scenario.steps["execute-tests"]

    assert isinstance(author, AgentScenarioStep)
    assert isinstance(executor, AgentScenarioStep)
    assert author.result_contract == "test_change"
    assert executor.result_contract == "test_execution"
    assert executor.model == "openai/gpt-4.1-mini"
    assert "Do not run" in author.prompt
    assert "Do not create, edit, delete" in executor.prompt
    assert "requirement coverage are invalid" in executor.prompt
    assert executor.transitions["FAILURE"] == "allow-test-rewrite"
    assert "Do not create a pull request" in author.prompt
    assert scenario.steps["await-user-decision"].decision == "merge"


def test_testing_event_inherits_exact_change_from_same_plane_issue(
    tmp_path: Path, image_resolver
):
    engine = build_engine(tmp_path, image_resolver, review_scenario())
    implementation = WorkflowInstance(
        id="wf-implementation",
        scenario_id="implement-ticket",
        scenario_version="3",
        trigger=TriggerEvent(
            source="plane",
            event="issue.ready_for_development",
            event_id="ready-1",
            data={
                "ticket": {"id": "issue-1"},
                "repository": {"full_name": "team/service"},
            },
        ),
        status="COMPLETED",
        outcome="SUCCESS",
        current_step=None,
        executions=[
            StepResult(
                step_id="implement",
                execution_id="implement-1",
                iteration=1,
                attempt=1,
                execution_status="COMPLETED",
                outcome="SUCCESS",
                data={
                    "implementation_change": {
                        "repository": "team/service",
                        "base_ref": "main",
                        "branch": "automation/wf-implementation",
                        "commit": "A" * 40,
                    }
                },
            )
        ],
    )
    engine.store.save(implementation)
    testing = TriggerEvent(
        source="plane",
        event="issue.testing",
        event_id="testing-1",
        data={
            "ticket": {"id": "issue-1"},
            "repository": {"full_name": "team/service", "implementation_ref": None},
        },
    )

    enriched = engine.attach_plane_implementation(testing)

    assert enriched.data["repository"] == {
        "full_name": "team/service",
        "implementation_ref": "automation/wf-implementation",
        "implementation_commit": "a" * 40,
        "implementation_workflow_id": "wf-implementation",
    }
    assert testing.data["repository"]["implementation_ref"] is None


def test_testing_event_without_prior_implementation_requires_explicit_ref(
    tmp_path: Path, image_resolver
):
    engine = build_engine(tmp_path, image_resolver, review_scenario())
    testing = TriggerEvent(
        source="plane",
        event="issue.testing",
        event_id="testing-1",
        data={
            "ticket": {"id": "issue-1"},
            "repository": {"full_name": "team/service", "implementation_ref": None},
        },
    )

    try:
        engine.attach_plane_implementation(testing)
    except WorkflowExecutionError as exc:
        assert str(exc) == "no completed implementation workflow was found for this Plane issue"
    else:
        raise AssertionError("missing implementation source must be rejected")


def test_testing_event_reads_external_developer_source_from_plane_fields(
    tmp_path: Path, image_resolver
):
    class StubPlane:
        def get_implementation_source(self, **kwargs):
            assert kwargs == {"project_id": "project-1", "issue_id": "issue-1"}
            return {
                "implementation_ref": "feature/payment-rules",
                "implementation_commit": "b" * 40,
            }

    engine = build_engine(tmp_path, image_resolver, review_scenario())
    engine.command_executor = CommandExecutor(plane_client=StubPlane())
    testing = TriggerEvent(
        source="plane",
        event="issue.testing",
        event_id="testing-external-1",
        data={
            "ticket": {"id": "issue-1"},
            "project": {"id": "project-1", "references": ["project-1", "PAY"]},
            "repository": {"full_name": "team/service", "implementation_ref": None},
        },
    )

    enriched = engine.attach_plane_implementation(testing)

    assert enriched.data["repository"] == {
        "full_name": "team/service",
        "implementation_ref": "feature/payment-rules",
        "implementation_commit": "b" * 40,
    }


def test_testing_scenario_passes_after_authored_tests_succeed(
    tmp_path: Path, image_resolver, monkeypatch
):
    registry = ScenarioRegistry(Path(__file__).parents[1] / "scenarios")
    service = AgentService(
        ContextBuilder(),
        image_resolver,
        SandboxManager(tmp_path / "jobs"),
    )
    class StubGitea:
        def verify_branch(self, **_kwargs):
            return None

        def create_final_pull_request(self, **kwargs):
            return {
                "repository": kwargs["repository"],
                "index": 9,
                "url": "http://gitea/team/service/pulls/9",
                "base": "main",
                "head": kwargs["head"],
                "commit": kwargs["commit"],
                "reused": False,
            }

    engine = WorkflowEngine(
        registry,
        WorkflowStore(tmp_path / "workflows"),
        service,
        command_executor=CommandExecutor(StubGitea()),
    )

    def run_agent(request):
        if request.step.id == "write-tests":
            return StepResult(
                step_id=request.step.id,
                execution_id=request.execution_id,
                iteration=request.iteration,
                attempt=request.attempt,
                execution_status="COMPLETED",
                outcome="SUCCESS",
                data={
                    "test_change": {
                        "repository": "team/service",
                        "base_ref": "feature/payment-retry",
                        "branch": f"automation/{request.workflow_id}",
                        "commit": "a" * 40,
                    }
                },
            )
        return StepResult(
            step_id=request.step.id,
            execution_id=request.execution_id,
            iteration=request.iteration,
            attempt=request.attempt,
            execution_status="COMPLETED",
            outcome="SUCCESS",
            data={
                "test_report": {
                    "verdict": "PASSED",
                    "repository": "team/service",
                    "branch": f"automation/{request.workflow_id}",
                    "commit": "a" * 40,
                    "command": "pytest -q",
                    "exit_code": 0,
                    "passed": 4,
                    "failed": 0,
                    "summary": "4 tests passed",
                }
            },
        )

    monkeypatch.setattr(service, "run", run_agent)
    waiting = engine.start(
        TriggerEvent(
            source="plane",
            event="issue.testing",
            event_id="testing-1",
            data={
                "repository": {
                    "full_name": "team/service",
                    "implementation_ref": "feature/payment-retry",
                }
            },
        )
    )

    assert waiting.status == "WAITING"
    assert waiting.pending_review is not None
    assert waiting.pending_review.decision == "merge"
    assert waiting.pending_review.pull_index == 9
    assert [execution.step_id for execution in waiting.executions] == [
        "write-tests",
        "execute-tests",
        "classify-test-run",
        "create-final-pull-request",
        "await-user-decision",
    ]
    assert waiting.executions[0].artifacts == []
    assert waiting.executions[3].artifacts[0].type == "pull_request"

    completed = engine.review(
        waiting.id,
        ReviewDecision(outcome="SUCCESS", comments=["Merged"]),
    )

    assert completed.status == "COMPLETED"
    assert completed.outcome == "SUCCESS"
    assert completed.executions[-1].data["plane_recommendation"] == (
        "accepted"
    )


def test_test_code_error_can_report_a_passing_but_incorrect_suite(
    tmp_path: Path, image_resolver
):
    service = AgentService(
        ContextBuilder(),
        image_resolver,
        SandboxManager(tmp_path / "jobs"),
    )
    engine = WorkflowEngine(
        ScenarioRegistry(Path(__file__).parents[1] / "scenarios"),
        WorkflowStore(tmp_path / "workflows"),
        service,
    )
    change = {
        "repository": "team/service",
        "base_ref": "feature/payment-retry",
        "branch": "automation/workflow-testing",
        "commit": "a" * 40,
        "pull_request": {"index": 9, "url": "http://gitea/team/service/pulls/9"},
    }
    workflow = WorkflowInstance(
        id="workflow-testing",
        scenario_id="test-ticket",
        scenario_version="1",
        trigger=TriggerEvent(
            source="plane",
            event="issue.testing",
            event_id="testing-invalid-coverage",
            data={
                "repository": {
                    "full_name": "team/service",
                    "implementation_ref": "feature/payment-retry",
                }
            },
        ),
        status="RUNNING",
        current_step="execute-tests",
        executions=[
            StepResult(
                step_id="write-tests",
                execution_id="write-tests-1",
                iteration=1,
                attempt=1,
                execution_status="COMPLETED",
                outcome="SUCCESS",
                data={"test_change": change},
            )
        ],
    )
    report = StepResult(
        step_id="execute-tests",
        execution_id="execute-tests-1",
        iteration=1,
        attempt=1,
        execution_status="COMPLETED",
        outcome="FAILURE",
        data={
            "test_report": {
                "verdict": "TEST_CODE_ERROR",
                "repository": change["repository"],
                "branch": change["branch"],
                "commit": change["commit"],
                "command": "python3 -m pytest -q",
                "exit_code": 0,
                "passed": 4,
                "failed": 0,
                "summary": "Suite passed, but an authored input does not match the requirement",
            }
        },
    )

    validated = engine._validate_test_execution_result(
        workflow, "execute-tests", 1, 1, report
    )

    assert validated.execution_status == "COMPLETED"
    assert validated.outcome == "FAILURE"
    assert validated.data["test_report"]["verdict"] == "TEST_CODE_ERROR"


def test_agent_cannot_replace_pull_request_during_review_iteration(
    tmp_path: Path, image_resolver
):
    scenario = {
        "id": "stable-pull",
        "trigger": {"source": "manual", "event": "stable-pull"},
        "start_step": "implement",
        "steps": {
            "implement": {
                "type": "agent",
                "prompt": "Implement",
                "model": "test",
                "transitions": {"SUCCESS": "review", "FAILURE": None},
            },
            "review": {
                "type": "review",
                "provider": "gitea",
                "transitions": {"SUCCESS": None, "FAILURE": "implement"},
            },
        },
    }
    engine = build_engine(tmp_path, image_resolver, scenario)
    workflow, _ = engine.create(
        TriggerEvent(
            source="manual",
            event="stable-pull",
            event_id="stable-pull-1",
        )
    )
    workflow.executions.append(
        StepResult(
            step_id="implement",
            execution_id="first",
            iteration=1,
            attempt=1,
            execution_status="COMPLETED",
            outcome="SUCCESS",
            data={
                "pull_request": {
                    "repository": "team/service",
                    "index": 17,
                    "url": "http://gitea/team/service/pulls/17",
                }
            },
            artifacts=[
                ArtifactRef(
                    type="pull_request",
                    uri="http://gitea/team/service/pulls/17",
                )
            ],
        )
    )
    step = engine.scenarios.get("stable-pull").steps["implement"]
    replacement = StepResult(
        step_id="implement",
        execution_id="second",
        iteration=2,
        attempt=1,
        execution_status="COMPLETED",
        outcome="SUCCESS",
        data={
            "pull_request": {
                "repository": "team/service",
                "index": 18,
                "url": "http://gitea/team/service/pulls/18",
            }
        },
        artifacts=[
            ArtifactRef(
                type="pull_request",
                uri="http://gitea/team/service/pulls/18",
            )
        ],
    )

    validated = engine._validate_agent_result(
        workflow, "implement", 2, 1, step, replacement
    )

    assert validated.execution_status == "ERROR"
    assert validated.outcome is None
    assert validated.error.code == "AGENT_PULL_REQUEST_CHANGED"


def test_successful_implementation_adds_missing_pull_request_artifact(
    tmp_path: Path, image_resolver
):
    scenario = {
        "id": "pull-contract",
        "trigger": {"source": "manual", "event": "pull-contract"},
        "start_step": "implement",
        "steps": {
            "implement": {
                "type": "agent",
                "prompt": "Implement",
                "model": "test",
                "transitions": {"SUCCESS": "review", "FAILURE": None},
            },
            "review": {
                "type": "review",
                "provider": "gitea",
                "transitions": {"SUCCESS": None, "FAILURE": "implement"},
            },
        },
    }
    engine = build_engine(tmp_path, image_resolver, scenario)
    workflow, _ = engine.create(
        TriggerEvent(
            source="manual",
            event="pull-contract",
            event_id="pull-contract-1",
        )
    )
    step = engine.scenarios.get("pull-contract").steps["implement"]
    result = StepResult(
        step_id="implement",
        execution_id="missing-artifact",
        iteration=1,
        attempt=1,
        execution_status="COMPLETED",
        outcome="SUCCESS",
        data={
            "pull_request": {
                "repository": "team/service",
                "index": 17,
                "url": "http://gitea/team/service/pulls/17",
            }
        },
    )

    validated = engine._validate_agent_result(
        workflow, "implement", 1, 1, step, result
    )

    assert validated.execution_status == "COMPLETED"
    assert validated.outcome == "SUCCESS"
    assert validated.artifacts[0].type == "pull_request"
    assert validated.artifacts[0].uri == "http://gitea/team/service/pulls/17"


def test_workflow_retries_technical_command_error(tmp_path: Path, image_resolver):
    scenario = {
        "id": "retry-flow",
        "trigger": {"source": "manual", "event": "retry"},
        "start_step": "broken",
        "steps": {
            "broken": {
                "type": "command",
                "command": "not-allowlisted",
                "retry": {"max_attempts": 2},
                "transitions": {"SUCCESS": None, "FAILURE": None},
            }
        },
    }
    engine = build_engine(tmp_path, image_resolver, scenario)
    now = [datetime(2026, 8, 20, tzinfo=UTC)]
    engine.clock = lambda: now[0]

    workflow = engine.start(
        TriggerEvent(source="manual", event="retry", event_id="event-2", data={})
    )

    assert workflow.status == "RUNNING"
    assert workflow.error.code == "COMMAND_ERROR"
    assert workflow.pending_retry is not None
    assert workflow.pending_retry.iteration == 1
    assert workflow.pending_retry.next_attempt == 2
    assert workflow.pending_retry.available_at == now[0] + timedelta(seconds=5)
    assert [result.attempt for result in workflow.executions] == [1]
    assert [change.status for change in workflow.executions[0].status_history] == [
        "PENDING",
        "READY",
        "RUNNING",
        "ERROR",
    ]

    unchanged = engine.advance(workflow)
    assert len(unchanged.executions) == 1

    now[0] += timedelta(seconds=5)
    failed = engine.advance(unchanged)

    assert failed.status == "FAILED"
    assert failed.pending_retry is None
    assert failed.error.code == "COMMAND_ERROR"
    assert failed.error.retryable is False
    assert [result.iteration for result in failed.executions] == [1, 1]
    assert [result.attempt for result in failed.executions] == [1, 2]


def test_failure_report_command_stores_failed_step_data():
    trigger = TriggerEvent(source="plane", event="issue", event_id="plane-1", data={})
    workflow = WorkflowInstance(
        id="workflow-1",
        scenario_id="implement-ticket",
        scenario_version="1",
        trigger=trigger,
        status="RUNNING",
        current_step="implementation-failed",
        executions=[
            StepResult(
                step_id="implement",
                execution_id="execution-1",
                iteration=1,
                attempt=1,
                execution_status="COMPLETED",
                outcome="FAILURE",
                data={"summary": "Repository is unavailable", "reason": "missing"},
                artifacts=[],
            )
        ],
    )
    step = CommandScenarioStep(
        type="command",
        command="store_failure_report",
        transitions={"SUCCESS": None, "FAILURE": None},
    )

    result = CommandExecutor().execute(
        workflow=workflow,
        step_id="implementation-failed",
        iteration=1,
        attempt=1,
        step=step,
    )

    assert result.outcome == "SUCCESS"
    assert result.data["failed_step"] == "implement"
    assert result.data["failure"]["reason"] == "missing"


def test_plane_sync_command_passes_workflow_result_details():
    calls = []

    class StubPlane:
        def record_result(self, **kwargs):
            calls.append(kwargs)
            return {"comment_created": True, "state_updated": False}

    workflow = WorkflowInstance(
        id="wf-plane",
        scenario_id="implement-ticket",
        scenario_version="3",
        trigger=TriggerEvent(
            source="plane",
            event="issue.ready_for_development",
            event_id="plane-sync-1",
            data={
                "ticket": {"id": "issue-1"},
                "project": {"id": "project-1"},
            },
        ),
        status="RUNNING",
        current_step="sync-ready-for-testing",
        executions=[
            StepResult(
                step_id="implement",
                execution_id="implement-1",
                iteration=1,
                attempt=1,
                execution_status="COMPLETED",
                outcome="SUCCESS",
                data={"implementation_change": {"commit": "a" * 40}},
            )
        ],
    )
    step = CommandScenarioStep(
        type="command",
        command="sync_plane_issue",
        parameters={
            "recommendation": "move_to_testing",
            "summary": "Ready for testing",
        },
        transitions={"SUCCESS": None, "FAILURE": None},
    )

    result = CommandExecutor(plane_client=StubPlane()).execute(
        workflow=workflow,
        step_id="sync-ready-for-testing",
        iteration=1,
        attempt=1,
        step=step,
    )

    assert result.outcome == "SUCCESS"
    assert calls[0]["project_id"] == "project-1"
    assert calls[0]["issue_id"] == "issue-1"
    assert calls[0]["details"]["implementation_change"]["commit"] == "a" * 40


def test_test_run_classifier_returns_product_failure():
    workflow = WorkflowInstance(
        id="workflow-testing",
        scenario_id="test-ticket",
        scenario_version="1",
        trigger=TriggerEvent(source="plane", event="issue.testing", event_id="testing-2"),
        status="RUNNING",
        current_step="classify-test-run",
        executions=[
            StepResult(
                step_id="execute-tests",
                execution_id="execute-tests-1",
                iteration=1,
                attempt=1,
                execution_status="COMPLETED",
                outcome="SUCCESS",
                data={"test_report": {"verdict": "PRODUCT_FAILURE", "failed": 2}},
            )
        ],
    )
    step = CommandScenarioStep(
        type="command",
        command="classify_test_run",
        parameters={"executor_step": "execute-tests"},
        transitions={"SUCCESS": None, "FAILURE": None},
    )

    result = CommandExecutor().execute(
        workflow=workflow,
        step_id="classify-test-run",
        iteration=1,
        attempt=1,
        step=step,
    )

    assert result.outcome == "FAILURE"
    assert result.data["test_report"]["verdict"] == "PRODUCT_FAILURE"


def test_workflow_deadline_and_manual_retry(tmp_path: Path, image_resolver):
    now = [datetime(2026, 8, 20, tzinfo=UTC)]
    scenario = review_scenario()
    scenario["timeout_seconds"] = 10
    engine = build_engine(tmp_path, image_resolver, scenario)
    engine.clock = lambda: now[0]
    workflow, _ = engine.create(
        TriggerEvent(source="manual", event="review", event_id="deadline-1", data={})
    )

    now[0] += timedelta(seconds=11)
    expired = engine.advance(workflow)

    assert expired.status == "FAILED"
    assert expired.error.code == "WORKFLOW_DEADLINE_EXCEEDED"
    assert expired.error.retryable is True

    retried = engine.retry(expired.id, reason="Temporary capacity issue")

    assert retried.status == "CREATED"
    assert retried.error is None
    assert retried.deadline_at == now[0] + timedelta(seconds=10)


def test_cancel_marker_prevents_stale_workflow_save(tmp_path: Path, image_resolver):
    engine = build_engine(tmp_path, image_resolver, review_scenario())
    workflow, _ = engine.create(
        TriggerEvent(source="manual", event="review", event_id="cancel-1", data={})
    )
    stale = workflow.model_copy(deep=True)

    cancelled = engine.cancel(workflow.id, reason="No longer needed")
    stale.status = "RUNNING"
    stale.current_step = "finish"
    saved = engine._save(stale)

    assert cancelled.status == "CANCELLED"
    assert cancelled.error.message == "No longer needed"
    assert saved.status == "CANCELLED"
    assert engine.get(workflow.id).status == "CANCELLED"


def test_cancel_marks_waiting_step_execution_as_cancelled(tmp_path: Path, image_resolver):
    engine = build_engine(tmp_path, image_resolver, review_scenario())
    waiting = engine.start(
        TriggerEvent(source="manual", event="review", event_id="cancel-waiting-1", data={})
    )

    cancelled = engine.cancel(waiting.id, reason="No longer needed")

    review = cancelled.executions[-1]
    assert cancelled.status == "CANCELLED"
    assert cancelled.pending_review is None
    assert review.step_id == "review"
    assert review.execution_status == "CANCELLED"
    assert review.error.code == "WORKFLOW_CANCELLED"
    assert [change.status for change in review.status_history] == [
        "PENDING",
        "READY",
        "WAITING",
        "CANCELLED",
    ]


def test_workflow_context_searches_only_bookstack_and_keeps_normalized_results(
    tmp_path: Path, image_resolver
):
    class FakeSwirlClient:
        def __init__(self):
            self.calls = []

        def search(self, query, *, providers, max_results):
            self.calls.append((query, providers, max_results))
            return SwirlSearchResponse(
                query=query,
                search_id="184",
                results=[
                    SwirlSearchResult(
                        title="Payment retry runbook",
                        snippet="Use a stable idempotency key.",
                        url="http://bookstack/books/payments/page/retry-runbook",
                        source="Local BookStack",
                    )
                ],
            )

    engine = build_engine(tmp_path, image_resolver, review_scenario())
    client = FakeSwirlClient()
    engine.swirl_client = client
    workflow, _ = engine.create(
        TriggerEvent(
            source="manual",
            event="review",
            event_id="bookstack-context-1",
            data={"ticket": {"summary": "payment retry"}},
        )
    )
    step = AgentScenarioStep(
        type="agent",
        prompt="Implement the ticket",
        plugins=["swirl"],
        model="test",
        context_search={
            "query_field": "ticket.summary",
            "providers": ["bookstack"],
            "max_results": 8,
        },
        transitions={"SUCCESS": None, "FAILURE": None},
    )

    context = engine._context(workflow, step)

    assert client.calls == [("payment retry", ["bookstack"], 8)]
    assert context.swirl_results == [
        {
            "title": "Payment retry runbook",
            "snippet": "Use a stable idempotency key.",
            "url": "http://bookstack/books/payments/page/retry-runbook",
            "source": "Local BookStack",
            "updated_at": None,
            "score": None,
        }
    ]


def test_swirl_error_is_a_technical_error_and_uses_retry_policy(tmp_path: Path, image_resolver):
    class FailingSwirlClient:
        def search(self, *_args, **_kwargs):
            raise SwirlSearchError("SWIRL request timed out")

    scenario = {
        "id": "swirl-retry",
        "trigger": {"source": "manual", "event": "swirl-retry"},
        "start_step": "search",
        "steps": {
            "search": {
                "type": "agent",
                "prompt": "Use relevant corporate context",
                "plugins": ["swirl"],
                "model": "test",
                "context_search": {
                    "query_field": "ticket.summary",
                    "providers": ["bookstack"],
                },
                "retry": {"max_attempts": 2},
                "transitions": {"SUCCESS": None, "FAILURE": None},
            }
        },
    }
    engine = build_engine(tmp_path, image_resolver, scenario)
    engine.swirl_client = FailingSwirlClient()
    now = [datetime(2026, 8, 20, tzinfo=UTC)]
    engine.clock = lambda: now[0]

    workflow = engine.start(
        TriggerEvent(
            source="manual",
            event="swirl-retry",
            event_id="swirl-error-1",
            data={"ticket": {"summary": "payment retry"}},
        )
    )

    assert workflow.status == "RUNNING"
    assert workflow.pending_retry is not None
    assert [result.execution_status for result in workflow.executions] == ["ERROR"]

    now[0] = workflow.pending_retry.available_at
    failed = engine.advance(workflow)

    assert failed.status == "FAILED"
    assert [result.execution_status for result in failed.executions] == ["ERROR", "ERROR"]
    assert [result.iteration for result in failed.executions] == [1, 1]
    assert [result.attempt for result in failed.executions] == [1, 2]
    assert all(result.outcome is None for result in failed.executions)
    assert all(result.error.retryable is True for result in failed.executions)
