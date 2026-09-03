from pathlib import Path

import pytest

from automation_orchestrator.context_builder import ContextBuilder
from automation_orchestrator.models import (
    AgentRunRequest,
    AgentStep,
    SandboxResult,
    WorkflowContext,
)
from automation_orchestrator.sandbox_manager import SandboxExecutionError, SandboxManager
from automation_orchestrator.service import AgentService


def test_prepare_creates_sandbox_task(image_resolver, tmp_path: Path):
    service = AgentService(
        ContextBuilder(),
        image_resolver,
        SandboxManager(tmp_path),
    )
    request = AgentRunRequest(
        execution_id="exec-1",
        workflow_id="wf-1",
        step=AgentStep(
            id="implement",
            prompt="Do the work",
            plugins=["git", "python"],
            model="test-model",
        ),
        context=WorkflowContext(trigger_data={"ticket": "A-1"}),
    )

    prepared = service.prepare(request)

    assert prepared.task["job_id"] == "exec-1"
    assert prepared.task["plugins"] == ["step-result"]
    assert prepared.plugins == ["step-result", "git", "python"]
    assert "skills" not in prepared.task
    assert prepared.task["context_digest"] == prepared.context.digest
    assert prepared.image == "automation-dsh-sandbox-code:0.1.0-rc.7"
    assert prepared.image_spec.profile == "code"
    assert prepared.required_commands == ["git", "python3"]
    assert "A-1" in prepared.task["prompt"]


def test_run_persists_context_usage_without_raw_prompt(
    image_resolver, tmp_path: Path, monkeypatch
):
    sandbox = SandboxManager(tmp_path)
    service = AgentService(ContextBuilder(), image_resolver, sandbox)
    request = AgentRunRequest(
        execution_id="exec-context-usage",
        workflow_id="wf-context-usage",
        step=AgentStep(
            id="investigate",
            prompt="Investigate the retry defect",
            model="test-model",
        ),
        context=WorkflowContext(
            trigger_data={
                "scope": "payment retries",
                "repository": {"full_name": "team/service", "ref": "main"},
            }
        ),
    )

    monkeypatch.setattr(
        sandbox,
        "run",
        lambda **_kwargs: SandboxResult(
            job_id=request.execution_id,
            status="success",
            summary="Investigation complete",
        ),
    )

    result = service.run(request)

    usage = result.data["context_usage"]
    assert usage["digest"]
    assert usage["character_count"] > 0
    assert {item["source"] for item in usage["sources"]} >= {
        "execution_contract",
        "task",
        "requirements",
        "repository",
    }
    assert "prompt" not in usage


def test_normalizes_native_business_failure():
    request = AgentRunRequest(
        execution_id="exec-failure",
        workflow_id="wf-1",
        step=AgentStep(id="implement", prompt="Do the work", model="test-model"),
    )
    sandbox_result = SandboxResult(
        job_id="exec-failure",
        status="failure",
        summary="Repository is unavailable",
        provider="openai",
        model="test-model",
        data={"reason": "repository_missing"},
        artifacts=[{"type": "report", "uri": "artifact://failure.md"}],
        error=None,
    )

    result = AgentService._normalize(request, sandbox_result)

    assert result.execution_status == "COMPLETED"
    assert result.outcome == "FAILURE"
    assert result.error is None
    assert result.data["reason"] == "repository_missing"
    assert result.artifacts[0].uri == "artifact://exec-failure/failure.md"


def test_normalizes_absolute_sandbox_output_artifact_uri():
    request = AgentRunRequest(
        execution_id="exec-output-artifact",
        workflow_id="wf-1",
        step=AgentStep(id="investigate", prompt="Investigate", model="test-model"),
    )
    sandbox_result = SandboxResult(
        job_id=request.execution_id,
        status="success",
        summary="Report created",
        artifacts=[
            {
                "type": "report",
                "uri": "/output/bug-report.md",
                "summary": "Bug report",
            }
        ],
    )

    result = AgentService._normalize(request, sandbox_result)

    assert result.artifacts[0].uri == (
        "artifact://exec-output-artifact/bug-report.md"
    )


def test_normalizes_shorthand_artifact_uri():
    request = AgentRunRequest(
        execution_id="exec-short-artifact",
        workflow_id="wf-1",
        step=AgentStep(id="investigate", prompt="Investigate", model="test-model"),
    )
    sandbox_result = SandboxResult(
        job_id=request.execution_id,
        status="success",
        summary="Report created",
        artifacts=[{"type": "report", "uri": "artifact://bug-report.md"}],
    )

    result = AgentService._normalize(request, sandbox_result)

    assert result.artifacts[0].uri == (
        "artifact://exec-short-artifact/bug-report.md"
    )


def test_normalizes_flat_bug_report_payload():
    request = AgentRunRequest(
        execution_id="exec-flat-bug-report",
        workflow_id="wf-1",
        step=AgentStep(
            id="find-bugs",
            prompt="Investigate",
            model="test-model",
        ),
    )
    report = {
        "repository": "team/service",
        "requested_ref": "main",
        "inspected_commit": "a" * 40,
        "status": "NO_FINDINGS",
        "report_path": "bug-report.md",
        "findings": [],
    }
    sandbox_result = SandboxResult(
        job_id=request.execution_id,
        status="success",
        summary="Investigation complete",
        data={**report, "diagnostic": "preserved"},
    )

    result = AgentService._normalize(request, sandbox_result)

    assert result.data["bug_report"] == report
    assert result.data["diagnostic"] == "preserved"
    assert "repository" not in result.data


def test_gitea_environment_is_allowlisted(image_resolver, monkeypatch, tmp_path: Path):
    resolution = image_resolver.resolve(
        plugin_names=["step-result", "gitea"],
    )
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("GITEA_BASE_URL", "http://gitea:3000")
    monkeypatch.setenv("GITEA_USERNAME", "harnes")
    monkeypatch.setenv("GITEA_TOKEN", "gitea-secret")
    monkeypatch.setenv("GITEA_ALLOWED_REPOSITORIES", "harnes/payments-api")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-pass")

    environment = SandboxManager(tmp_path)._environment(resolution)

    assert environment == {
        "DSH_PERMISSION_MODE": "danger-full-access",
        "OPENAI_API_KEY": "openai-secret",
        "GITEA_BASE_URL": "http://gitea:3000",
        "GITEA_USERNAME": "harnes",
        "GITEA_TOKEN": "gitea-secret",
        "GITEA_ALLOWED_REPOSITORIES": "harnes/payments-api",
    }


def test_gitea_environment_is_required(image_resolver, monkeypatch, tmp_path: Path):
    resolution = image_resolver.resolve(
        plugin_names=["step-result", "gitea"],
    )
    monkeypatch.delenv("GITEA_BASE_URL", raising=False)
    monkeypatch.delenv("GITEA_TOKEN", raising=False)

    with pytest.raises(SandboxExecutionError, match="GITEA_BASE_URL"):
        SandboxManager(tmp_path)._environment(resolution)


def test_openrouter_environment_is_scoped(image_resolver, monkeypatch, tmp_path: Path):
    resolution = image_resolver.resolve(
        plugin_names=["step-result"],
    )
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-secret")

    environment = SandboxManager(tmp_path)._environment(resolution, "openrouter")

    assert environment == {
        "DSH_PERMISSION_MODE": "danger-full-access",
        "OPENROUTER_API_KEY": "openrouter-secret",
    }


def test_swirl_environment_is_scoped(image_resolver, monkeypatch, tmp_path: Path):
    resolution = image_resolver.resolve(
        plugin_names=["step-result", "swirl"],
    )
    monkeypatch.setenv("SWIRL_BASE_URL", "http://swirl:8000")
    monkeypatch.setenv("SWIRL_USERNAME", "agent")
    monkeypatch.setenv("SWIRL_PASSWORD", "swirl-secret")
    monkeypatch.setenv("SWIRL_CONTENT_ALLOWED_ORIGINS", "http://bookstack")
    monkeypatch.setenv("SWIRL_CONTENT_ROUTES_JSON", "[]")
    monkeypatch.setenv("GITEA_TOKEN", "must-not-pass")

    environment = SandboxManager(tmp_path)._environment(resolution)

    assert environment == {
        "DSH_PERMISSION_MODE": "danger-full-access",
        "SWIRL_BASE_URL": "http://swirl:8000",
        "SWIRL_USERNAME": "agent",
        "SWIRL_PASSWORD": "swirl-secret",
        "SWIRL_CONTENT_ALLOWED_ORIGINS": "http://bookstack",
        "SWIRL_CONTENT_ROUTES_JSON": "[]",
    }
