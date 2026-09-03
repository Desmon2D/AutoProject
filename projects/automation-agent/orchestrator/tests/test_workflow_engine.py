import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from automation_orchestrator.context_builder import ContextBuilder
from automation_orchestrator.models import (
    AgentRunRequest,
    AgentScenarioStep,
    AgentStep,
    ArtifactRef,
    CommandScenarioStep,
    ReviewDecision,
    StepResult,
    SwirlSearchResponse,
    SwirlSearchResult,
    TriggerEvent,
    WorkflowContext,
    WorkflowInstance,
)
from automation_orchestrator.sandbox_manager import SandboxManager
from automation_orchestrator.scenario_registry import ScenarioRegistry
from automation_orchestrator.service import AgentService
from automation_orchestrator.swirl_client import SwirlSearchError
from automation_orchestrator.test_runner import TestRun
from automation_orchestrator.workflow_engine import (
    CommandExecutor,
    WorkflowEngine,
    WorkflowExecutionError,
)
from automation_orchestrator.workflow_search import focused_search_terms
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


def test_waiting_workflow_uses_immutable_scenario_snapshot_after_upgrade(
    tmp_path: Path, image_resolver
):
    scenario = review_scenario()
    scenario["version"] = "1"
    engine = build_engine(tmp_path, image_resolver, scenario)
    waiting = engine.start(
        TriggerEvent(source="manual", event="review", event_id="snapshot-review", data={})
    )
    snapshot_path = engine.store.root / waiting.id / "scenario.json"

    upgraded = review_scenario()
    upgraded["version"] = "2"
    upgraded["steps"]["review"]["transitions"]["SUCCESS"] = "new-finish"
    upgraded["steps"]["new-finish"] = {
        "type": "command",
        "command": "complete",
        "parameters": {"summary": "new graph"},
        "transitions": {"SUCCESS": None, "FAILURE": None},
    }
    (engine.scenarios.root / "scenario.json").write_text(
        json.dumps(upgraded), encoding="utf-8"
    )
    upgraded_engine = WorkflowEngine(
        ScenarioRegistry(engine.scenarios.root),
        engine.store,
        engine.agent_service,
    )

    completed = upgraded_engine.review(
        waiting.id,
        ReviewDecision(outcome="SUCCESS", comments=["Approved on old version"]),
    )

    snapshot = engine.store.get_scenario_snapshot(waiting.id)
    assert snapshot_path.is_file()
    assert snapshot is not None
    assert completed.scenario_version == "1"
    assert completed.scenario_snapshot_sha256 == engine.store.scenario_digest(snapshot)
    assert [execution.step_id for execution in completed.executions] == [
        "prepare",
        "review",
        "finish",
    ]


def test_plane_review_refreshes_ticket_before_returning_to_development(
    tmp_path: Path, image_resolver
):
    scenario = {
        "id": "plane-development-review",
        "trigger": {"source": "plane", "event": "issue.ready_for_development"},
        "start_step": "review",
        "steps": {
            "review": {
                "type": "review",
                "provider": "plane",
                "transitions": {"SUCCESS": "finish", "FAILURE": "finish"},
            },
            "finish": {
                "type": "command",
                "command": "complete",
                "transitions": {"SUCCESS": None, "FAILURE": None},
            },
        },
    }
    engine = build_engine(tmp_path, image_resolver, scenario)
    waiting = engine.start(
        TriggerEvent(
            source="plane",
            event="issue.ready_for_development",
            event_id="ready-original",
            data={"ticket": {"id": "issue-1", "description": "Original"}},
        )
    )
    refreshed = TriggerEvent(
        source="plane",
        event="issue.ready_for_development",
        event_id="ready-revised",
        data={"ticket": {"id": "issue-1", "description": "Revised requirements"}},
    )

    resumed = engine.review(
        waiting.id,
        ReviewDecision(outcome="FAILURE", comments=["Please revise"]),
        advance=False,
        refreshed_trigger=refreshed,
    )

    assert resumed.status == "RUNNING"
    assert resumed.current_step == "finish"
    assert resumed.trigger.event_id == "ready-revised"
    assert resumed.trigger.data["ticket"]["description"] == "Revised requirements"
    assert resumed.review_comments == ["Please revise"]


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

    completed = engine.start(TriggerEvent(source="manual", event="failure", event_id="failure-1"))

    assert completed.status == "COMPLETED"
    assert completed.outcome == "FAILURE"
    assert completed.error is None


def test_implementation_failure_does_not_use_approval_terminal():
    registry = ScenarioRegistry(Path(__file__).parents[1] / "scenarios")
    scenario = registry.get("implement-ticket")

    assert scenario.stage == "development"
    assert scenario.version == "6"
    assert scenario.start_step == "sync-development-started"
    assert scenario.timeout_seconds == 604800
    assert scenario.title == "Разработка задачи"
    implement_step = scenario.steps["implement"]
    assert isinstance(implement_step, AgentScenarioStep)
    assert implement_step.context_search.query_field == "ticket.search_query"
    assert implement_step.context_search.fetch_content is True
    assert "Build a checklist" in implement_step.prompt
    review_step = scenario.steps["await-development-review"]
    assert review_step.provider == "plane"
    assert review_step.transitions == {
        "SUCCESS": "sync-approved-for-testing",
        "FAILURE": "sync-development-started",
    }
    report_step = scenario.steps["implementation-failed"]
    failure_step = scenario.steps["finish-failed"]

    assert report_step.transitions == {
        "SUCCESS": "sync-implementation-failed",
        "FAILURE": "sync-implementation-failed",
    }
    assert isinstance(failure_step, CommandScenarioStep)
    assert failure_step.command == "fail"
    assert "testable branch" in failure_step.parameters["summary"]


def test_testing_scenario_uses_agent_author_and_deterministic_executor():
    registry = ScenarioRegistry(Path(__file__).parents[1] / "scenarios")
    scenario = registry.get("test-ticket")

    assert scenario.stage == "testing"
    assert scenario.version == "5"
    author = scenario.steps["write-tests"]
    executor = scenario.steps["execute-tests"]

    assert isinstance(author, AgentScenarioStep)
    assert isinstance(executor, CommandScenarioStep)
    assert author.result_contract == "test_change"
    assert executor.command == "execute_test_change"
    assert "never execute tests" in author.prompt
    assert "first action MUST be calling gitea_get_repository" in author.prompt
    assert author.model == "openai/gpt-4.1"
    assert executor.transitions["FAILURE"] == "allow-test-rewrite"
    assert "make no file changes" in author.prompt
    assert scenario.steps["await-user-decision"].decision == "merge"


def test_bug_finding_scenario_separates_investigation_from_verification():
    registry = ScenarioRegistry(Path(__file__).parents[1] / "scenarios")
    scenario = registry.get("bug-finding")

    assert scenario.stage == "bug-finding"
    assert scenario.version == "3"
    assert scenario.trigger.event == "bug-finding.requested"
    finder = scenario.steps["find-bugs"]
    verifier = scenario.steps["verify-reproducers"]

    assert isinstance(finder, AgentScenarioStep)
    assert isinstance(verifier, CommandScenarioStep)
    assert finder.result_contract == "bug_report"
    assert finder.plugins == ["git", "gitea", "swirl", "python"]
    assert "never modify or push product code" in finder.prompt
    assert verifier.command == "verify_bug_report"
    assert verifier.transitions["FAILURE"] == "allow-report-rewrite"


def test_bug_report_contract_requires_structured_reproducer_and_report(
    tmp_path: Path, image_resolver
):
    registry = ScenarioRegistry(Path(__file__).parents[1] / "scenarios")
    service = AgentService(
        ContextBuilder(), image_resolver, SandboxManager(tmp_path / "jobs")
    )
    engine = WorkflowEngine(
        registry,
        WorkflowStore(tmp_path / "workflows"),
        service,
    )
    workflow, _ = engine.create(
        TriggerEvent(
            source="manual",
            event="bug-finding.requested",
            event_id="bug-contract-1",
            data={
                "repository": {"full_name": "team/service", "ref": "main"},
                "scope": "payment retry",
                "search_query": "payment retry",
            },
        )
    )
    step = registry.get("bug-finding").steps["find-bugs"]
    execution_id = "bug-contract-execution"
    output = tmp_path / "jobs" / execution_id / "output"
    output.mkdir(parents=True)
    output.joinpath("bug-report.md").write_text(
        "# Bug report\n\n## BUG-001: retry counter\n\n"
        "A deterministic reproducer demonstrates that the retry counter is incremented twice. "
        "The inspected implementation violates the expected single increment and the report "
        "records the exact source evidence and command.\n",
        encoding="utf-8",
    )
    result = StepResult(
        step_id="find-bugs",
        execution_id=execution_id,
        iteration=1,
        attempt=1,
        execution_status="COMPLETED",
        outcome="SUCCESS",
        data={
            "bug_report": {
                "repository": "team/service",
                "requested_ref": "main",
                "inspected_commit": "a" * 40,
                "status": "FOUND",
                "report_path": "bug-report.md",
                "findings": [
                    {
                        "id": "BUG-001",
                        "title": "Retry counter increments twice",
                        "severity": "medium",
                        "confidence": "high",
                        "evidence": [
                            {
                                "path": "src/retry.py",
                                "line": 17,
                                "description": "Both branches increment the same counter",
                            }
                        ],
                        "reproduction": {
                            "steps": ["Run the focused Pytest reproducer"],
                            "expected": "Counter equals one",
                            "actual": "Counter equals two",
                        },
                        "root_cause": "The success branch duplicates the common increment",
                        "reproducer": {
                            "path": "reproducers/test_bug_001.py",
                            "command": ["python3", "-m", "pytest", "reproducers/test_bug_001.py"],
                            "content": "def test_retry_counter():\n    assert 2 == 1\n",
                        },
                    }
                ],
            }
        },
        artifacts=[ArtifactRef(type="file", uri="artifact://bug-report.md")],
    )

    validated = engine._validate_agent_result(
        workflow, "find-bugs", 1, 1, step, result
    )

    assert validated.execution_status == "COMPLETED"
    assert validated.artifacts[0].type == "report"
    assert validated.artifacts[0].uri == (
        f"artifact://{execution_id}/bug-report.md"
    )

    invalid = result.model_copy(
        update={
            "data": {
                "bug_report": {
                    **result.data["bug_report"],
                    "findings": [
                        {
                            **result.data["bug_report"]["findings"][0],
                            "reproducer": {"path": "../unsafe.py", "command": ["pytest"], "content": "x"},
                        }
                    ],
                }
            }
        }
    )
    rejected = engine._validate_agent_result(
        workflow, "find-bugs", 1, 1, step, invalid
    )

    assert rejected.execution_status == "ERROR"
    assert rejected.error.code == "AGENT_BUG_FINDING_INVALID"


def test_bug_report_verifier_uses_clean_revision_and_overlay():
    report = {
        "repository": "team/service",
        "requested_ref": "main",
        "inspected_commit": "b" * 40,
        "status": "FOUND",
        "report_path": "bug-report.md",
        "findings": [
            {
                "id": "BUG-001",
                "reproducer": {
                    "path": "reproducers/test_bug_001.py",
                    "command": ["pytest", "reproducers/test_bug_001.py"],
                    "content": "def test_bug():\n    assert False\n",
                },
            }
        ],
    }
    workflow = WorkflowInstance(
        id="wf-bug-verification",
        scenario_id="bug-finding",
        scenario_version="1",
        trigger=TriggerEvent(
            source="manual",
            event="bug-finding.requested",
            event_id="bug-verification-1",
        ),
        status="RUNNING",
        current_step="verify-reproducers",
        executions=[
            StepResult(
                step_id="find-bugs",
                execution_id="finder-1",
                iteration=1,
                attempt=1,
                execution_status="COMPLETED",
                outcome="SUCCESS",
                data={"bug_report": report},
            )
        ],
    )
    calls = []

    class StubGitea:
        def download_archive(self, **kwargs):
            assert kwargs == {"repository": "team/service", "commit": "b" * 40}
            return b"clean-archive"

    class StubRunner:
        def run(self, archive, command, *, overlay_files=None):
            calls.append((archive, command, overlay_files))
            return TestRun(
                command=command,
                exit_code=1,
                passed=0,
                failed=1,
                verdict="PRODUCT_FAILURE",
                summary="1 failed",
                output="assertion failed",
                framework="pytest",
                total=1,
            )

    step = CommandScenarioStep(
        type="command",
        command="verify_bug_report",
        parameters={"author_step": "find-bugs"},
        transitions={"SUCCESS": None, "FAILURE": None},
    )
    result = CommandExecutor(StubGitea(), test_runner=StubRunner()).execute(
        workflow=workflow,
        step_id="verify-reproducers",
        iteration=1,
        attempt=1,
        step=step,
    )

    assert result.outcome == "SUCCESS"
    assert result.data["bug_verification"]["status"] == "VERIFIED"
    assert result.data["bug_verification"]["authoritative"] is True
    assert calls == [
        (
            b"clean-archive",
            ["pytest", "reproducers/test_bug_001.py"],
            {"reproducers/test_bug_001.py": b"def test_bug():\n    assert False\n"},
        )
    ]


def test_testing_event_inherits_exact_change_from_same_plane_issue(tmp_path: Path, image_resolver):
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


def test_ready_event_after_product_failure_inherits_test_branch_and_feedback(
    tmp_path: Path, image_resolver
):
    engine = build_engine(tmp_path, image_resolver, review_scenario())
    failed_test = WorkflowInstance(
        id="wf-testing",
        scenario_id="test-ticket",
        scenario_version="3",
        trigger=TriggerEvent(
            source="plane",
            event="issue.testing",
            event_id="testing-product-failure",
            data={
                "ticket": {"id": "issue-1"},
                "repository": {"full_name": "team/service"},
            },
        ),
        status="RUNNING",
        current_step="sync-product-failed",
        executions=[
            StepResult(
                step_id="write-tests",
                execution_id="write-tests-1",
                iteration=1,
                attempt=1,
                execution_status="COMPLETED",
                outcome="SUCCESS",
                data={
                    "test_change": {
                        "repository": "team/service",
                        "branch": "automation/wf-testing",
                        "commit": "c" * 40,
                    }
                },
            ),
            StepResult(
                step_id="execute-tests",
                execution_id="execute-tests-1",
                iteration=1,
                attempt=1,
                execution_status="COMPLETED",
                outcome="SUCCESS",
                data={
                    "test_report": {
                        "verdict": "PRODUCT_FAILURE",
                        "command": ["python3", "-m", "pytest"],
                        "exit_code": 1,
                        "passed": 6,
                        "failed": 1,
                        "summary": "one assertion failed",
                        "output": "failure details",
                    }
                },
            ),
        ],
    )
    engine.store.save(failed_test)
    ready = TriggerEvent(
        source="plane",
        event="issue.ready_for_development",
        event_id="ready-rework",
        data={
            "ticket": {"id": "issue-1"},
            "repository": {"full_name": "team/service", "implementation_ref": None},
        },
    )

    enriched = engine.attach_plane_implementation(ready)

    assert enriched.data["repository"]["implementation_ref"] == "automation/wf-testing"
    assert enriched.data["repository"]["implementation_commit"] == "c" * 40
    assert enriched.data["repository"]["selection_source"] == "failed_test_workflow"
    assert enriched.data["development_feedback"]["failed"] == 1
    assert enriched.data["development_feedback"]["output"] == "failure details"


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
        def get_repository_source(self, **kwargs):
            assert kwargs == {"project_id": "project-1", "issue_id": "issue-1"}

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

    ensured_branches = []

    class StubGitea:
        def ensure_branch(self, **kwargs):
            ensured_branches.append(kwargs)

        def verify_branch(self, **_kwargs):
            return None

        def verify_descendant(self, **_kwargs):
            return None

        def download_archive(self, **_kwargs):
            return b"archive"

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

    class StubTestRunner:
        def run(self, _archive, command):
            return TestRun(
                command=command,
                exit_code=0,
                passed=4,
                failed=0,
                verdict="PASSED",
                summary="Authoritative test run passed (4 passed)",
                output="4 passed",
            )

    engine = WorkflowEngine(
        registry,
        WorkflowStore(tmp_path / "workflows"),
        service,
        command_executor=CommandExecutor(StubGitea(), test_runner=StubTestRunner()),
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
                        "base_commit": "b" * 40,
                        "branch": f"automation/{request.workflow_id}",
                        "commit": "b" * 40,
                        "command": ["python3", "-m", "pytest"],
                        "changed": False,
                    }
                },
            )
        raise AssertionError("only the test author may call the model")

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
                    "implementation_commit": "b" * 40,
                }
            },
        )
    )

    assert waiting.status == "WAITING"
    assert waiting.pending_review is not None
    assert waiting.pending_review.decision == "merge"
    assert waiting.pending_review.pull_index == 9
    assert ensured_branches == [
        {
            "repository": "team/service",
            "branch": f"automation/{waiting.id}",
            "commit": "b" * 40,
        }
    ]
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
    assert completed.executions[-1].data["plane_recommendation"] == ("accepted")


def test_test_code_error_can_report_a_passing_but_incorrect_suite(tmp_path: Path, image_resolver):
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
                "command": ["python3", "-m", "pytest", "-q"],
                "exit_code": 0,
                "total": 4,
                "passed": 4,
                "failed": 0,
                "errors": 0,
                "summary": "Suite passed, but an authored input does not match the requirement",
            }
        },
    )

    validated = engine._validate_test_execution_result(workflow, "execute-tests", 1, 1, report)

    assert validated.execution_status == "COMPLETED"
    assert validated.outcome == "FAILURE"
    assert validated.data["test_report"]["verdict"] == "TEST_CODE_ERROR"


def test_markdown_contract_rejects_declared_but_missing_document(tmp_path: Path, image_resolver):
    scenario = {
        "id": "analysis-document",
        "trigger": {"source": "manual", "event": "analysis.requested"},
        "start_step": "analyze",
        "steps": {
            "analyze": {
                "type": "agent",
                "prompt": "Analyze documentation",
                "model": "test",
                "result_contract": "markdown_document",
                "transitions": {"SUCCESS": None, "FAILURE": None},
            }
        },
    }
    engine = build_engine(tmp_path, image_resolver, scenario)
    workflow, _ = engine.create(
        TriggerEvent(
            source="manual",
            event="analysis.requested",
            event_id="analysis-missing-file",
            data={"request": "Prepare requirements"},
        )
    )
    step = engine.scenarios.get("analysis-document").steps["analyze"]
    result = StepResult(
        step_id="analyze",
        execution_id="analysis-missing-file-1",
        iteration=1,
        attempt=1,
        execution_status="COMPLETED",
        outcome="SUCCESS",
        data={
            "document": {
                "title": "Requirements",
                "format": "markdown",
                "path": "analysis.md",
            }
        },
        artifacts=[ArtifactRef(type="document", uri="artifact://analysis.md")],
    )

    validated = engine._validate_agent_result(workflow, "analyze", 1, 1, step, result)

    assert validated.execution_status == "ERROR"
    assert validated.error.code == "AGENT_MARKDOWN_ARTIFACT_MISSING"


def test_markdown_contract_rejects_document_without_source_citation(
    tmp_path: Path, image_resolver
):
    scenario = {
        "id": "analysis-document",
        "trigger": {"source": "manual", "event": "analysis.requested"},
        "start_step": "analyze",
        "steps": {
            "analyze": {
                "type": "agent",
                "prompt": "Analyze documentation",
                "model": "test",
                "result_contract": "markdown_document",
                "transitions": {"SUCCESS": None, "FAILURE": None},
            }
        },
    }
    engine = build_engine(tmp_path, image_resolver, scenario)
    workflow, _ = engine.create(
        TriggerEvent(
            source="manual",
            event="analysis.requested",
            event_id="analysis-missing-citation",
            data={"request": "Prepare requirements"},
        )
    )
    step = engine.scenarios.get("analysis-document").steps["analyze"]
    execution_id = "analysis-missing-citation-1"
    output = tmp_path / "jobs" / execution_id / "output"
    output.mkdir(parents=True)
    output.joinpath("analysis.md").write_text(
        "# Requirements\n\n## Purpose\n\n" + "Substantive uncited analysis. " * 8,
        encoding="utf-8",
    )
    result = StepResult(
        step_id="analyze",
        execution_id=execution_id,
        iteration=1,
        attempt=1,
        execution_status="COMPLETED",
        outcome="SUCCESS",
        data={
            "document": {
                "title": "Requirements",
                "format": "markdown",
                "path": "analysis.md",
            }
        },
        artifacts=[ArtifactRef(type="document", uri="artifact://analysis.md")],
    )

    validated = engine._validate_agent_result(workflow, "analyze", 1, 1, step, result)

    assert validated.execution_status == "ERROR"
    assert validated.error.code == "AGENT_MARKDOWN_CITATION_MISSING"


def test_development_event_uses_allowed_repository_link_from_plane(
    tmp_path: Path, image_resolver
):
    class StubPlane:
        def get_repository_source(self, **kwargs):
            assert kwargs == {"project_id": "project-1", "issue_id": "issue-1"}
            return {
                "full_name": "team/other-service",
                "source_url": "http://localhost:3000/team/other-service",
            }

    class StubGitea:
        def __init__(self):
            self.allowed_repositories = {"team/service", "team/other-service"}

    engine = build_engine(tmp_path, image_resolver, review_scenario())
    engine.command_executor = CommandExecutor(
        gitea_client=StubGitea(),
        plane_client=StubPlane(),
    )
    ready = TriggerEvent(
        source="plane",
        event="issue.ready_for_development",
        event_id="ready-linked-repository",
        data={
            "ticket": {"id": "issue-1"},
            "project": {"id": "project-1", "references": ["project-1", "PAY"]},
            "repository": {
                "full_name": "team/service",
                "implementation_ref": None,
                "selection_source": "project_mapping",
            },
        },
    )

    enriched = engine.attach_plane_implementation(ready)

    assert enriched.data["repository"] == {
        "full_name": "team/other-service",
        "implementation_ref": None,
        "selection_source": "plane_link",
        "source_url": "http://localhost:3000/team/other-service",
    }
    assert ready.data["repository"]["full_name"] == "team/service"


def test_development_event_rejects_repository_link_outside_allowlist(
    tmp_path: Path, image_resolver
):
    class StubPlane:
        def get_repository_source(self, **_kwargs):
            return {
                "full_name": "external/unsafe",
                "source_url": "http://localhost:3000/external/unsafe",
            }

    class StubGitea:
        def __init__(self):
            self.allowed_repositories = {"team/service"}

    engine = build_engine(tmp_path, image_resolver, review_scenario())
    engine.command_executor = CommandExecutor(
        gitea_client=StubGitea(),
        plane_client=StubPlane(),
    )
    ready = TriggerEvent(
        source="plane",
        event="issue.ready_for_development",
        event_id="ready-unsafe-repository",
        data={
            "ticket": {"id": "issue-1"},
            "project": {"id": "project-1"},
            "repository": {"full_name": "team/service"},
        },
    )

    with pytest.raises(WorkflowExecutionError, match="GITEA_ALLOWED_REPOSITORIES"):
        engine.attach_plane_implementation(ready)


def test_markdown_contract_requires_citation_for_primary_topic_source(
    tmp_path: Path, image_resolver
):
    scenario = {
        "id": "analysis-document",
        "trigger": {"source": "manual", "event": "analysis.requested"},
        "start_step": "analyze",
        "steps": {
            "analyze": {
                "type": "agent",
                "prompt": "Analyze documentation",
                "model": "test",
                "result_contract": "markdown_document",
                "transitions": {"SUCCESS": None, "FAILURE": None},
            }
        },
    }
    engine = build_engine(tmp_path, image_resolver, scenario)
    workflow, _ = engine.create(
        TriggerEvent(
            source="manual",
            event="analysis.requested",
            event_id="analysis-wrong-source",
            data={"request": "Prepare confidentiality requirements"},
        )
    )
    step = engine.scenarios.get("analysis-document").steps["analyze"]
    execution_id = "analysis-wrong-source-1"
    engine.agent_service.job_store.begin(
        AgentRunRequest(
            execution_id=execution_id,
            workflow_id=workflow.id,
            step=AgentStep(id="analyze", prompt="Analyze documentation", model="test"),
            context=WorkflowContext(
                retrieval_summary={"primary_topic_terms": ["confidentiality"]},
                swirl_results=[
                    {
                        "title": "Security controls",
                        "url": "https://kb.example/security",
                        "excerpts": [
                            {
                                "text": "Confidentiality controls",
                                "matched_terms": ["confidentiality"],
                            }
                        ],
                    }
                ],
            ),
        )
    )
    output = tmp_path / "jobs" / execution_id / "output"
    output.mkdir(parents=True)
    output.joinpath("analysis.md").write_text(
        "# Confidentiality requirements\n\n"
        "The document discusses confidentiality controls but cites the wrong source. "
        "Further substantive content is included for contract validation.\n\n"
        "[Generic source](https://kb.example/generic)\n",
        encoding="utf-8",
    )
    result = StepResult(
        step_id="analyze",
        execution_id=execution_id,
        iteration=1,
        attempt=1,
        execution_status="COMPLETED",
        outcome="SUCCESS",
        data={
            "document": {
                "title": "Confidentiality requirements",
                "format": "markdown",
                "path": "analysis.md",
            }
        },
        artifacts=[ArtifactRef(type="document", uri="artifact://analysis.md")],
    )

    validated = engine._validate_agent_result(workflow, "analyze", 1, 1, step, result)

    assert validated.execution_status == "ERROR"
    assert validated.error.code == "AGENT_MARKDOWN_PRIMARY_SOURCE_MISSING"


def test_agent_cannot_replace_pull_request_during_review_iteration(tmp_path: Path, image_resolver):
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

    validated = engine._validate_agent_result(workflow, "implement", 2, 1, step, replacement)

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

    validated = engine._validate_agent_result(workflow, "implement", 1, 1, step, result)

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
            "document_id": None,
            "content": None,
            "excerpts": [],
            "content_fetched": False,
            "content_format": None,
            "content_truncated": False,
            "retrieval_score": 0.01639344,
            "matched_queries": ["full_query"],
            "updated_at": None,
            "score": None,
        }
    ]


def test_agent_context_keeps_resolved_node_inputs(tmp_path: Path, image_resolver):
    engine = build_engine(tmp_path, image_resolver, review_scenario())
    workflow, _ = engine.create(
        TriggerEvent(
            source="manual",
            event="review",
            event_id="agent-node-inputs-1",
            data={},
        )
    )
    step = AgentScenarioStep(
        type="agent",
        prompt="Implement the ticket",
        model="test",
        transitions={"SUCCESS": None, "FAILURE": None},
    )

    context = engine._context(workflow, step, node_inputs={"ticket": {"id": "A-1"}})

    assert context.node_inputs == {"ticket": {"id": "A-1"}}


def test_workflow_context_fetches_full_source_text(tmp_path: Path, image_resolver):
    class FakeSwirlClient:
        def search(self, query, *, providers, max_results):
            return SwirlSearchResponse(
                query=query,
                results=[
                    SwirlSearchResult(
                        title="Security requirements",
                        snippet="Short preview",
                        url="http://bookstack/books/analytics/page/security",
                        source="Local BookStack",
                        document_id="17",
                    )
                ],
            )

        def fetch_document(self, result, *, max_characters):
            assert max_characters == 8000
            return result.model_copy(
                update={
                    "content": "# Security\n\nFull authoritative source.",
                    "content_format": "markdown",
                }
            )

    engine = build_engine(tmp_path, image_resolver, review_scenario())
    engine.swirl_client = FakeSwirlClient()
    workflow, _ = engine.create(
        TriggerEvent(
            source="manual",
            event="review",
            event_id="bookstack-full-context-1",
            data={"search_query": "security requirements"},
        )
    )
    step = AgentScenarioStep(
        type="agent",
        prompt="Analyze the documentation",
        plugins=["swirl"],
        model="test",
        context_search={
            "query_field": "search_query",
            "providers": ["bookstack"],
            "fetch_content": True,
            "max_content_documents": 3,
            "max_content_characters": 8000,
            "min_content_documents": 1,
        },
        transitions={"SUCCESS": None, "FAILURE": None},
    )

    context = engine._context(workflow, step)

    assert context.swirl_results[0]["document_id"] == "17"
    assert context.swirl_results[0]["content"] is None
    assert context.swirl_results[0]["content_fetched"] is True
    assert context.swirl_results[0]["excerpts"][0]["heading"] == "Security"
    assert context.swirl_results[0]["excerpts"][0]["text"].endswith(
        "authoritative source."
    )
    assert context.swirl_results[0]["content_format"] == "markdown"


def test_workflow_context_falls_back_to_topic_terms_when_full_query_is_empty(
    tmp_path: Path, image_resolver
):
    class FakeSwirlClient:
        def __init__(self):
            self.calls = []

        def search(self, query, *, providers, max_results):
            self.calls.append((query, providers, max_results))
            results = []
            if query == "конфиденциальность":
                results = [
                    SwirlSearchResult(
                        title="Нефункциональные требования и безопасность",
                        snippet="Токены и пароли не записываются в журнал.",
                        url="http://bookstack/books/analytics/page/security",
                        source="Local BookStack",
                    )
                ]
            return SwirlSearchResponse(query=query, search_id=query, results=results)

    engine = build_engine(tmp_path, image_resolver, review_scenario())
    client = FakeSwirlClient()
    engine.swirl_client = client
    query = (
        "Изучи документацию и сформулируй меры сохранения "
        "конфиденциальности информации"
    )
    workflow, _ = engine.create(
        TriggerEvent(
            source="manual",
            event="review",
            event_id="bookstack-context-fallback-1",
            data={"search_query": query},
        )
    )
    step = AgentScenarioStep(
        type="agent",
        prompt="Analyze the documentation",
        plugins=["swirl"],
        model="test",
        context_search={
            "query_field": "search_query",
            "providers": ["bookstack"],
            "max_results": 8,
            "fallback_on_empty": True,
            "max_fallback_queries": 4,
        },
        transitions={"SUCCESS": None, "FAILURE": None},
    )

    context = engine._context(workflow, step)

    assert client.calls == [(query, ["bookstack"], 8)] + [
        (term, ["bookstack"], 8)
        for term in focused_search_terms(query, max_queries=4)
    ]
    assert [item["title"] for item in context.swirl_results] == [
        "Нефункциональные требования и безопасность"
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
