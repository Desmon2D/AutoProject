import hashlib
import hmac
import json
from pathlib import Path

from fastapi.testclient import TestClient

from automation_orchestrator.api import create_app
from automation_orchestrator.context_builder import ContextBuilder
from automation_orchestrator.models import (
    StepError,
    StepResult,
    SwirlSearchResponse,
    SwirlSearchResult,
)
from automation_orchestrator.sandbox_manager import SandboxManager
from automation_orchestrator.scenario_registry import ScenarioRegistry
from automation_orchestrator.service import AgentService
from automation_orchestrator.worker import process_one
from automation_orchestrator.workflow_engine import WorkflowEngine
from automation_orchestrator.workflow_store import WorkflowStore


def test_health_plugins_and_prepare_endpoints(image_resolver, tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("DEFAULT_AGENT_PROVIDER", raising=False)
    monkeypatch.delenv("DEFAULT_AGENT_MODEL", raising=False)
    sandbox = SandboxManager(tmp_path)
    sandbox.is_available = lambda: True
    service = AgentService(
        ContextBuilder(),
        image_resolver,
        sandbox,
    )
    client = TestClient(create_app(service))
    payload = {
        "execution_id": "exec-api-1",
        "workflow_id": "wf-api-1",
        "step": {
            "id": "implement",
            "prompt": "Do the work",
            "plugins": ["git"],
            "model": "test-model",
        },
        "context": {"trigger_data": {"ticket": "A-1"}},
    }

    health = client.get("/health").json()
    assert health["status"] == "ok"
    assert health["docker"] is True
    assert health["providers"]["openai"]["configured"] is False
    assert health["providers"]["openrouter"]["configured"] is False
    assert health["default_agent"]["model"] == "z-ai/glm-4.7-flash"
    assert health["queue"]["pending"] == 0
    assert health["queue"]["worker_online"] is False
    assert "gitea" in health["providers"]
    assert any(item["name"] == "step-result" for item in client.get("/v1/plugins").json())
    assert any(item["name"] == "git" for item in client.get("/v1/skills").json())
    assert any(item["name"] == "python" for item in client.get("/v1/capabilities").json())
    assert any(item["name"] == "code" for item in client.get("/v1/images").json())
    response = client.post("/v1/agent-steps/prepare", json=payload)
    assert response.status_code == 200
    assert response.json()["task"]["plugins"] == ["step-result"]
    assert response.json()["plugins"] == ["step-result", "git"]
    assert "skills" not in response.json()["task"]
    assert response.json()["image_spec"]["profile"] == "code"


def test_trigger_and_review_workflow_endpoints(image_resolver, tmp_path: Path):
    sandbox = SandboxManager(tmp_path / "jobs")
    service = AgentService(ContextBuilder(), image_resolver, sandbox)
    service.scenario_registry = ScenarioRegistry(Path(__file__).parents[1] / "scenarios")
    service.workflow_engine = WorkflowEngine(
        service.scenario_registry,
        WorkflowStore(tmp_path / "workflows"),
        service,
    )
    client = TestClient(create_app(service))

    response = client.post(
        "/v1/triggers",
        json={
            "source": "manual",
            "event": "review-demo",
            "event_id": "api-event-1",
            "data": {"ticket": "A-1"},
        },
    )
    assert response.status_code == 202
    created = response.json()
    assert created["status"] == "CREATED"
    assert process_one(service, worker_id="review-worker", heartbeat_seconds=0.01)
    waiting = client.get(f"/v1/workflows/{created['id']}").json()
    assert waiting["status"] == "WAITING"
    assert waiting["current_step"] == "review"
    listed = client.get("/v1/workflows")
    assert listed.status_code == 200
    assert [workflow["id"] for workflow in listed.json()] == [waiting["id"]]

    reviewed = client.post(
        f"/v1/workflows/{waiting['id']}/review",
        json={"outcome": "SUCCESS", "comments": ["Approved"]},
    )
    assert reviewed.status_code == 202
    assert reviewed.json()["status"] == "RUNNING"
    assert process_one(service, worker_id="review-worker", heartbeat_seconds=0.01)
    completed = client.get(f"/v1/workflows/{waiting['id']}")
    assert completed.status_code == 200
    assert completed.json()["status"] == "COMPLETED"


def test_cancel_pending_workflow_endpoint(image_resolver, tmp_path: Path):
    sandbox = SandboxManager(tmp_path / "jobs")
    service = AgentService(ContextBuilder(), image_resolver, sandbox)
    service.scenario_registry = ScenarioRegistry(Path(__file__).parents[1] / "scenarios")
    service.workflow_engine = WorkflowEngine(
        service.scenario_registry,
        WorkflowStore(tmp_path / "workflows"),
        service,
    )
    client = TestClient(create_app(service))
    created = client.post(
        "/v1/triggers",
        json={
            "source": "manual",
            "event": "review-demo",
            "event_id": "cancel-api-1",
            "data": {},
        },
    ).json()

    response = client.post(
        f"/v1/workflows/{created['id']}/cancel",
        json={"reason": "Ticket withdrawn"},
    )

    assert response.status_code == 202
    assert response.json()["status"] == "CANCELLED"
    assert response.json()["error"]["code"] == "WORKFLOW_CANCELLED"
    assert service.workflow_queue.get(created["id"]).status == "COMPLETED"
    assert process_one(service, worker_id="cancel-worker", heartbeat_seconds=0.01) is False
    events = client.get(f"/v1/audit-events?resource_id={created['id']}").json()
    assert any(item["action"] == "workflow.cancelled" for item in events)


def test_retry_failed_workflow_endpoint(image_resolver, tmp_path: Path):
    sandbox = SandboxManager(tmp_path / "jobs")
    service = AgentService(ContextBuilder(), image_resolver, sandbox)
    service.scenario_registry = ScenarioRegistry(Path(__file__).parents[1] / "scenarios")
    service.workflow_engine = WorkflowEngine(
        service.scenario_registry,
        WorkflowStore(tmp_path / "workflows"),
        service,
    )
    client = TestClient(create_app(service))
    created = client.post(
        "/v1/triggers",
        json={
            "source": "manual",
            "event": "review-demo",
            "event_id": "retry-api-1",
            "data": {},
        },
    ).json()
    assert client.post(
        f"/v1/workflows/{created['id']}/retry",
        json={"reason": "Too early"},
    ).status_code == 409

    assert service.workflow_queue.cancel_pending(created["id"])
    failed = service.workflow_engine.get(created["id"])
    failed.status = "FAILED"
    failed.error = StepError(code="TEST_FAILURE", message="Temporary failure", retryable=True)
    service.workflow_engine.store.save(failed)

    response = client.post(
        f"/v1/workflows/{created['id']}/retry",
        json={"reason": "Capacity restored"},
    )

    assert response.status_code == 202
    assert response.json()["status"] == "CREATED"
    assert response.json()["error"] is None
    assert service.workflow_queue.get(created["id"]).status == "PENDING"
    events = client.get(f"/v1/audit-events?resource_id={created['id']}").json()
    assert any(item["action"] == "workflow.retry.requested" for item in events)


def test_signed_gitea_webhook_is_idempotent(image_resolver, tmp_path: Path, monkeypatch):
    sandbox = SandboxManager(tmp_path / "jobs")
    service = AgentService(ContextBuilder(), image_resolver, sandbox)
    service.scenario_registry = ScenarioRegistry(Path(__file__).parents[1] / "scenarios")
    service.workflow_engine = WorkflowEngine(
        service.scenario_registry,
        WorkflowStore(tmp_path / "workflows"),
        service,
    )
    agent_requests = []

    def run_agent(request):
        agent_requests.append(request)
        return StepResult(
            step_id=request.step.id,
            execution_id=request.execution_id,
            iteration=request.iteration,
            attempt=request.attempt,
            execution_status="COMPLETED",
            outcome="SUCCESS",
            data={
                "summary": "Push analyzed",
                "provider": request.step.provider,
                "model": request.step.model,
            },
            artifacts=[],
        )

    monkeypatch.setattr(service, "run", run_agent)
    client = TestClient(create_app(service))
    secret = "test-webhook-secret"
    monkeypatch.setenv("GITEA_WEBHOOK_SECRET", secret)
    body = json.dumps(
        {
            "ref": "refs/heads/main",
            "before": "a" * 40,
            "after": "b" * 40,
            "repository": {
                "full_name": "harnes/payments-api",
                "html_url": "http://gitea:3000/harnes/payments-api",
                "clone_url": "http://gitea:3000/harnes/payments-api.git",
                "default_branch": "main",
            },
            "pusher": {"username": "harnes"},
            "commits": [{"id": "b" * 40, "message": "test push"}],
        },
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Gitea-Event-Type": "push",
        "X-Gitea-Delivery": "delivery-1",
        "X-Gitea-Signature": signature,
    }

    first = client.post("/v1/webhooks/gitea", content=body, headers=headers)
    duplicate = client.post("/v1/webhooks/gitea", content=body, headers=headers)

    assert first.status_code == 202
    assert first.json()["status"] == "CREATED"
    assert first.json()["trigger"]["data"]["repository"]["full_name"] == ("harnes/payments-api")
    assert service.workflow_queue.get(first.json()["id"]).status == "PENDING"
    assert len(agent_requests) == 0
    assert process_one(service, worker_id="test-worker", heartbeat_seconds=0.01)
    completed = client.get(f"/v1/workflows/{first.json()['id']}").json()
    assert completed["status"] == "COMPLETED"
    assert completed["executions"][0]["step_id"] == "analyze-push"
    assert len(agent_requests) == 1
    assert agent_requests[0].step.provider == "openrouter"
    assert agent_requests[0].step.model == "openai/gpt-4.1-nano"
    assert agent_requests[0].context.trigger_data["commits"][0]["message"] == "test push"
    assert service.workflow_queue.get(first.json()["id"]).status == "COMPLETED"
    assert duplicate.json()["id"] == first.json()["id"]
    assert len(client.get("/v1/workflows").json()) == 1
    assert (
        client.post(
            "/v1/webhooks/gitea",
            content=body,
            headers={**headers, "X-Gitea-Signature": "0" * 64},
        ).status_code
        == 401
    )

    invalid_body = json.dumps(
        {"ref": "refs/heads/main", "repository": {}, "commits": []},
        separators=(",", ":"),
    ).encode()
    invalid_signature = hmac.new(secret.encode(), invalid_body, hashlib.sha256).hexdigest()
    invalid = client.post(
        "/v1/webhooks/gitea",
        content=invalid_body,
        headers={
            **headers,
            "X-Gitea-Delivery": "delivery-2",
            "X-Gitea-Signature": invalid_signature,
        },
    )
    assert invalid.status_code == 422

    automation_payload = json.loads(body)
    automation_payload["ref"] = "refs/heads/automation/wf-test"
    automation_body = json.dumps(automation_payload, separators=(",", ":")).encode()
    automation_signature = hmac.new(
        secret.encode(), automation_body, hashlib.sha256
    ).hexdigest()
    ignored = client.post(
        "/v1/webhooks/gitea",
        content=automation_body,
        headers={
            **headers,
            "X-Gitea-Delivery": "delivery-automation",
            "X-Gitea-Signature": automation_signature,
        },
    )
    assert ignored.status_code == 202
    assert ignored.json() == {
        "accepted": False,
        "reason": "automation branch push is handled by its parent workflow",
    }
    assert len(client.get("/v1/workflows").json()) == 1


def test_signed_plane_webhook_normalizes_ready_issue_and_is_idempotent(
    image_resolver, tmp_path: Path, monkeypatch
):
    sandbox = SandboxManager(tmp_path / "jobs")
    service = AgentService(ContextBuilder(), image_resolver, sandbox)
    service.scenario_registry = ScenarioRegistry(Path(__file__).parents[1] / "scenarios")
    service.workflow_engine = WorkflowEngine(
        service.scenario_registry,
        WorkflowStore(tmp_path / "workflows"),
        service,
    )
    client = TestClient(create_app(service))
    secret = "plane-webhook-secret"
    monkeypatch.setenv("PLANE_WEBHOOK_SECRET", secret)
    monkeypatch.setenv("PLANE_READY_STATE_NAMES", "Ready for development")
    monkeypatch.setenv("PLANE_TESTING_STATE_NAMES", "Testing")
    monkeypatch.setenv(
        "PLANE_PROJECT_REPOSITORIES",
        json.dumps({"PAY": "team/service"}),
    )
    payload = {
        "event": "issue",
        "action": "update",
        "webhook_id": "webhook-1",
        "workspace_id": "workspace-1",
        "data": {
            "id": "issue-1",
            "sequence_id": 17,
            "name": "Fix payment retry",
            "description_stripped": "Retry transient failures",
            "updated_at": "2026-08-20T10:00:00Z",
            "project": {"id": "project-1", "identifier": "PAY"},
            "state_detail": {"id": "state-ready", "name": "Ready for development"},
        },
        "activity": {"id": "activity-1"},
    }
    body = json.dumps(payload).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Plane-Event": "issue",
        "X-Plane-Delivery": "delivery-1",
        "X-Plane-Signature": signature,
    }

    first = client.post("/v1/webhooks/plane", content=body, headers=headers)
    duplicate = client.post(
        "/v1/webhooks/plane",
        content=body,
        headers={**headers, "X-Plane-Delivery": "delivery-2"},
    )

    assert first.status_code == 202
    assert first.json()["accepted"] is True
    assert first.json()["created"] is True
    assert duplicate.status_code == 202
    assert duplicate.json()["created"] is False
    workflow = first.json()["workflow"]
    assert workflow["trigger"]["source"] == "plane"
    assert workflow["trigger"]["event"] == "issue.ready_for_development"
    assert workflow["trigger"]["data"]["ticket"]["summary"] == "Fix payment retry"
    assert workflow["trigger"]["data"]["repository"]["full_name"] == "team/service"
    assert len(client.get("/v1/workflows").json()) == 1
    assert (
        client.post(
            "/v1/webhooks/plane",
            content=body,
            headers={**headers, "X-Plane-Signature": "0" * 64},
        ).status_code
        == 401
    )

    payload["data"]["state_detail"] = {"id": "state-testing", "name": "Testing"}
    payload["data"]["updated_at"] = "2026-08-20T10:01:00Z"
    payload["data"]["description_stripped"] = (
        "Test the implemented retry behavior\n\n"
        "Automation implementation ref: feature/payment-retry"
    )
    testing_body = json.dumps(payload).encode()
    testing_signature = hmac.new(secret.encode(), testing_body, hashlib.sha256).hexdigest()
    testing = client.post(
        "/v1/webhooks/plane",
        content=testing_body,
        headers={**headers, "X-Plane-Signature": testing_signature},
    )
    assert testing.status_code == 202
    assert testing.json()["accepted"] is True
    assert testing.json()["workflow"]["trigger"]["event"] == "issue.testing"
    assert testing.json()["workflow"]["trigger"]["data"]["repository"] == {
        "full_name": "team/service",
        "implementation_ref": "feature/payment-retry",
    }

    payload["data"]["state_detail"] = {"id": "state-backlog", "name": "Backlog"}
    payload["data"]["updated_at"] = "2026-08-20T10:02:00Z"
    ignored_body = json.dumps(payload).encode()
    ignored_signature = hmac.new(secret.encode(), ignored_body, hashlib.sha256).hexdigest()
    ignored = client.post(
        "/v1/webhooks/plane",
        content=ignored_body,
        headers={**headers, "X-Plane-Signature": ignored_signature},
    )
    assert ignored.status_code == 202
    assert ignored.json() == {
        "accepted": False,
        "reason": "issue is not in an actionable state",
    }


def test_signed_gitea_review_webhook_resumes_waiting_workflow(
    image_resolver, tmp_path: Path, monkeypatch
):
    sandbox = SandboxManager(tmp_path / "jobs")
    service = AgentService(ContextBuilder(), image_resolver, sandbox)
    service.scenario_registry = ScenarioRegistry(Path(__file__).parents[1] / "scenarios")
    service.workflow_engine = WorkflowEngine(
        service.scenario_registry,
        WorkflowStore(tmp_path / "workflows"),
        service,
    )
    client = TestClient(create_app(service))
    created = client.post(
        "/v1/triggers",
        json={
            "source": "manual",
            "event": "review-demo",
            "event_id": "review-webhook-1",
            "data": {},
        },
    ).json()
    assert process_one(service, worker_id="review-webhook-worker", heartbeat_seconds=0.01)
    waiting = client.get(f"/v1/workflows/{created['id']}").json()
    secret = "test-webhook-secret"
    monkeypatch.setenv("GITEA_WEBHOOK_SECRET", secret)
    body = json.dumps(
        {
            "action": "reviewed",
            "repository": {"full_name": "team/service"},
            "pull_request": {
                "number": 17,
                "html_url": "http://gitea/team/service/pulls/17",
                "body": f"Changes\n\n<!-- automation-workflow: {waiting['id']} -->",
            },
            "review": {"type": "approved", "content": "Looks good"},
        },
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Gitea-Event-Type": "pull_request_review_approved",
        "X-Gitea-Delivery": "review-delivery-1",
        "X-Gitea-Signature": signature,
    }

    reviewed = client.post("/v1/webhooks/gitea", content=body, headers=headers)
    duplicate = client.post("/v1/webhooks/gitea", content=body, headers=headers)

    assert reviewed.status_code == 202
    assert reviewed.json()["status"] == "RUNNING"
    assert reviewed.json()["review_comments"] == ["Looks good"]
    assert reviewed.json()["processed_event_ids"] == ["review-delivery-1"]
    assert duplicate.status_code == 202
    assert duplicate.json()["processed_event_ids"] == ["review-delivery-1"]
    assert process_one(service, worker_id="review-webhook-worker", heartbeat_seconds=0.01)
    completed = client.get(f"/v1/workflows/{waiting['id']}").json()
    assert completed["status"] == "COMPLETED"


def test_gitea_merge_decision_ignores_approval_and_handles_merge_or_close(
    image_resolver, tmp_path: Path, monkeypatch
):
    scenario_root = tmp_path / "merge-scenarios"
    scenario_root.mkdir()
    (scenario_root / "merge.json").write_text(
        json.dumps(
            {
                "id": "merge-decision",
                "trigger": {"source": "manual", "event": "merge-decision"},
                "start_step": "create-pull",
                "steps": {
                    "create-pull": {
                        "type": "command",
                        "command": "complete",
                        "parameters": {
                            "data": {
                                "pull_request": {
                                    "repository": "team/service",
                                    "index": 17,
                                    "url": "http://gitea/team/service/pulls/17",
                                }
                            }
                        },
                        "transitions": {"SUCCESS": "decision", "FAILURE": None},
                    },
                    "decision": {
                        "type": "review",
                        "provider": "gitea",
                        "decision": "merge",
                        "transitions": {"SUCCESS": "accepted", "FAILURE": "rejected"},
                    },
                    "accepted": {
                        "type": "command",
                        "command": "complete",
                        "transitions": {"SUCCESS": None, "FAILURE": None},
                    },
                    "rejected": {
                        "type": "command",
                        "command": "fail",
                        "transitions": {"SUCCESS": None, "FAILURE": None},
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    service = AgentService(
        ContextBuilder(), image_resolver, SandboxManager(tmp_path / "merge-jobs")
    )
    service.scenario_registry = ScenarioRegistry(scenario_root)
    service.workflow_engine = WorkflowEngine(
        service.scenario_registry,
        WorkflowStore(tmp_path / "merge-workflows"),
        service,
    )
    secret = "merge-webhook-secret"
    monkeypatch.setenv("GITEA_WEBHOOK_SECRET", secret)
    client = TestClient(create_app(service))

    for sequence, merged in enumerate((True, False), start=1):
        created = client.post(
            "/v1/triggers",
            json={
                "source": "manual",
                "event": "merge-decision",
                "event_id": f"merge-decision-{sequence}",
                "data": {},
            },
        ).json()
        assert process_one(service, worker_id=f"merge-worker-{sequence}", heartbeat_seconds=0.01)
        waiting = client.get(f"/v1/workflows/{created['id']}").json()
        assert waiting["status"] == "WAITING"
        assert waiting["pending_review"]["decision"] == "merge"

        approval_body = json.dumps(
            {
                "action": "reviewed",
                "repository": {"full_name": "team/service"},
                "pull_request": {
                    "number": 17,
                    "body": f"<!-- automation-workflow: {waiting['id']} -->",
                },
                "review": {"type": "approved"},
            },
            separators=(",", ":"),
        ).encode()
        approval_signature = hmac.new(
            secret.encode(), approval_body, hashlib.sha256
        ).hexdigest()
        ignored = client.post(
            "/v1/webhooks/gitea",
            content=approval_body,
            headers={
                "Content-Type": "application/json",
                "X-Gitea-Event-Type": "pull_request_review_approved",
                "X-Gitea-Delivery": f"merge-approval-{sequence}",
                "X-Gitea-Signature": approval_signature,
            },
        )
        assert ignored.status_code == 202
        assert ignored.json()["status"] == "WAITING"

        close_body = json.dumps(
            {
                "action": "closed",
                "repository": {"full_name": "team/service"},
                "pull_request": {
                    "number": 17,
                    "merged": merged,
                    "html_url": "http://gitea/team/service/pulls/17",
                    "body": f"<!-- automation-workflow: {waiting['id']} -->",
                },
            },
            separators=(",", ":"),
        ).encode()
        close_signature = hmac.new(secret.encode(), close_body, hashlib.sha256).hexdigest()
        decided = client.post(
            "/v1/webhooks/gitea",
            content=close_body,
            headers={
                "Content-Type": "application/json",
                "X-Gitea-Event-Type": "pull_request",
                "X-Gitea-Delivery": f"merge-close-{sequence}",
                "X-Gitea-Signature": close_signature,
            },
        )
        assert decided.status_code == 202
        assert decided.json()["status"] == "RUNNING"
        assert process_one(
            service, worker_id=f"merge-finish-{sequence}", heartbeat_seconds=0.01
        )
        completed = client.get(f"/v1/workflows/{waiting['id']}").json()
        assert completed["status"] == "COMPLETED"
        assert completed["outcome"] == ("SUCCESS" if merged else "FAILURE")


def test_implement_ticket_produces_branch_ready_for_testing(
    image_resolver, tmp_path: Path, monkeypatch
):
    sandbox = SandboxManager(tmp_path / "jobs")
    service = AgentService(ContextBuilder(), image_resolver, sandbox)
    service.scenario_registry = ScenarioRegistry(Path(__file__).parents[1] / "scenarios")

    class StubSwirl:
        def __init__(self):
            self.queries = []

        def search(self, query, **_kwargs):
            self.queries.append(query)
            return SwirlSearchResponse(
                query=query,
                results=[
                    SwirlSearchResult(
                        title="Ticket requirements",
                        snippet="Reference only",
                        url="https://kb/A-1",
                        source="Confluence",
                    )
                ],
            )

    swirl = StubSwirl()
    service.workflow_engine = WorkflowEngine(
        service.scenario_registry,
        WorkflowStore(tmp_path / "workflows"),
        service,
        swirl_client=swirl,
    )
    agent_requests = []

    def run_agent(request):
        agent_requests.append(request)
        return StepResult(
            step_id=request.step.id,
            execution_id=request.execution_id,
            iteration=request.iteration,
            attempt=request.attempt,
            execution_status="COMPLETED",
            outcome="SUCCESS",
            data={
                "summary": "Changes pushed",
                "implementation_change": {
                    "repository": "team/service",
                    "base_ref": "main",
                    "branch": f"automation/{request.workflow_id}",
                    "commit": "a" * 40,
                },
            },
        )

    monkeypatch.setattr(service, "run", run_agent)
    secret = "test-webhook-secret"
    monkeypatch.setenv("GITEA_WEBHOOK_SECRET", secret)
    plane_secret = "plane-webhook-secret"
    monkeypatch.setenv("PLANE_WEBHOOK_SECRET", plane_secret)
    monkeypatch.setenv("PLANE_READY_STATE_NAMES", "Ready for development")
    monkeypatch.setenv("PLANE_TESTING_STATE_NAMES", "Testing")
    monkeypatch.setenv(
        "PLANE_PROJECT_REPOSITORIES",
        json.dumps({"PAY": "team/service"}),
    )
    client = TestClient(create_app(service))
    plane_payload = {
        "event": "issue",
        "action": "update",
        "webhook_id": "ticket-webhook",
        "workspace_id": "workspace-1",
        "data": {
            "id": "A-1",
            "name": "Fix payment retry",
            "updated_at": "2026-08-20T10:00:00Z",
            "project": {"identifier": "PAY"},
            "state_detail": {
                "id": "ready-state",
                "name": "Ready for development",
            },
        },
        "activity": None,
    }
    plane_body = json.dumps(plane_payload).encode()
    plane_signature = hmac.new(plane_secret.encode(), plane_body, hashlib.sha256).hexdigest()
    plane_response = client.post(
        "/v1/webhooks/plane",
        content=plane_body,
        headers={
            "Content-Type": "application/json",
            "X-Plane-Event": "issue",
            "X-Plane-Delivery": "ticket-delivery-1",
            "X-Plane-Signature": plane_signature,
        },
    )
    assert plane_response.status_code == 202
    created = plane_response.json()["workflow"]
    assert process_one(service, worker_id="ticket-worker", heartbeat_seconds=0.01)
    completed = client.get(f"/v1/workflows/{created['id']}").json()
    assert completed["status"] == "COMPLETED"
    assert completed["executions"][-1]["data"]["plane_recommendation"] == "move_to_testing"
    assert len(agent_requests) == 1
    assert agent_requests[0].context.swirl_results[0]["url"] == "https://kb/A-1"
    assert swirl.queries == ["Fix payment retry"]

    plane_payload["data"]["state_detail"] = {
        "id": "testing-state",
        "name": "Testing",
    }
    plane_payload["data"]["updated_at"] = "2026-08-20T10:01:00Z"
    testing_body = json.dumps(plane_payload).encode()
    testing_signature = hmac.new(
        plane_secret.encode(), testing_body, hashlib.sha256
    ).hexdigest()
    testing_response = client.post(
        "/v1/webhooks/plane",
        content=testing_body,
        headers={
            "Content-Type": "application/json",
            "X-Plane-Event": "issue",
            "X-Plane-Delivery": "ticket-delivery-2",
            "X-Plane-Signature": testing_signature,
        },
    )

    assert testing_response.status_code == 202
    source = testing_response.json()["workflow"]["trigger"]["data"]["repository"]
    assert source == {
        "full_name": "team/service",
        "implementation_ref": f"automation/{created['id']}",
        "implementation_commit": "a" * 40,
        "implementation_workflow_id": created["id"],
    }
