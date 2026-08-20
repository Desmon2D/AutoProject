import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from automation_orchestrator.context_builder import ContextBuilder
from automation_orchestrator.models import (
    AgentScenarioStep,
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
from automation_orchestrator.workflow_engine import CommandExecutor, WorkflowEngine
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
        "SUCCESS": "finish-failed",
        "FAILURE": "finish-failed",
    }
    assert isinstance(failure_step, CommandScenarioStep)
    assert failure_step.command == "fail"
    assert "without an approved pull request" in failure_step.parameters["summary"]


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
