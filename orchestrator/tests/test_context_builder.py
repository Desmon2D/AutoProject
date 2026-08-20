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


def test_context_builder_honors_size_limit():
    step = AgentStep(id="small", prompt="x" * 1000, model="test")
    result = ContextBuilder(max_characters=200).build(step, WorkflowContext())
    assert result.character_count <= 200
    assert result.truncated is True
