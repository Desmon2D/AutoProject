from automation_orchestrator.context_builder import ContextBuilder
from automation_orchestrator.models import (
    AgentStep,
    ArtifactRef,
    PreviousStepResult,
    WorkflowContext,
)


def test_context_builder_selects_relevant_data_and_redacts_secrets():
    step = AgentStep(id="implement", prompt="Implement the fix", plugins=["git"], model="test")
    context = WorkflowContext(
        trigger_data={"issue": "A-1", "api_token": "secret-value"},
        previous_steps=[
            PreviousStepResult(
                step_id="analyze",
                execution_status="COMPLETED",
                outcome="SUCCESS",
                data={"summary": "Root cause found", "stdout": "very noisy"},
                artifacts=[ArtifactRef(type="report", uri="artifact://wf/report.md")],
            )
        ],
        review_comments=["Please add a regression test"],
        swirl_results=[
            {
                "title": "Payment retry runbook",
                "snippet": "Use a stable idempotency key.",
                "url": "http://bookstack/books/payments/page/retry-runbook",
                "source": "Local BookStack",
            }
        ],
    )

    result = ContextBuilder().build(step, context)

    assert "Implement the fix" in result.prompt
    assert "Root cause found" in result.prompt
    assert "artifact://wf/report.md" in result.prompt
    assert "Please add a regression test" in result.prompt
    assert "secret-value" not in result.prompt
    assert "[REDACTED]" in result.prompt
    assert "very noisy" not in result.prompt
    assert "Payment retry runbook" in result.prompt
    assert "untrusted reference data" in result.prompt
    assert result.digest


def test_context_builder_includes_resolved_node_inputs():
    result = ContextBuilder().build(
        AgentStep(id="implement", prompt="Implement", model="test"),
        WorkflowContext(node_inputs={"ticket": {"id": "A-1"}}),
    )

    assert "node_inputs" in result.included_sources
    assert '"id": "A-1"' in result.prompt


def test_context_builder_honors_size_limit():
    step = AgentStep(id="small", prompt="x" * 1000, model="test")
    result = ContextBuilder(max_characters=200).build(step, WorkflowContext())
    assert result.character_count <= 200
    assert result.truncated is True


def test_context_builder_separates_sources_and_reports_usage():
    step = AgentStep(id="investigate", prompt="Find the defect", model="test")
    context = WorkflowContext(
        trigger_data={
            "scope": "payment retries",
            "symptoms": "duplicate charge",
            "repository": {"full_name": "team/service", "ref": "main"},
        },
        scenario={"workflow_id": "wf-1", "current_step": "find-bugs"},
        previous_steps=[
            PreviousStepResult(
                step_id="verify",
                execution_status="COMPLETED",
                outcome="FAILURE",
                data={"summary": "Reproducer was invalid"},
            )
        ],
        review_comments=["Focus on timeout handling"],
        retrieval_summary={"topic_coverage_sufficient": True},
        swirl_results=[
            {
                "title": "Retry policy",
                "url": "https://kb.example/retry",
                "excerpts": [{"text": "A payment is retried once."}],
            }
        ],
    )

    result = ContextBuilder().build(step, context)

    assert result.prompt.index("# Execution contract") < result.prompt.index("# Task")
    assert "# Trigger requirements and inputs" in result.prompt
    assert "# Repository context from Trigger data" in result.prompt
    assert result.prompt.count('"full_name": "team/service"') == 1
    reports = {item.source: item for item in result.source_report}
    assert reports["requirements"].category == "requirements"
    assert reports["repository"].category == "repository"
    assert reports["review_comments"].category == "review"
    assert reports["previous_steps"].category == "history"
    assert reports["swirl_results"].category == "documentation"
    assert all(not item.omitted for item in result.source_report)


def test_context_builder_reports_omitted_low_priority_sources():
    result = ContextBuilder(max_characters=500).build(
        AgentStep(id="small", prompt="Inspect carefully", model="test"),
        WorkflowContext(
            trigger_data={"scope": "x" * 2000},
            swirl_results=[{"title": "doc", "url": "https://kb", "snippet": "y" * 2000}],
        ),
    )

    reports = {item.source: item for item in result.source_report}
    assert reports["execution_contract"].omitted is False
    assert reports["swirl_results"].omitted is True
    assert reports["swirl_results"].included_characters == 0
    assert result.truncated is True
