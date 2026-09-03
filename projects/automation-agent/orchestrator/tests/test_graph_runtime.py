import hashlib
import hmac
import json
from pathlib import Path

from fastapi.testclient import TestClient

from automation_orchestrator.api import create_app
from automation_orchestrator.context_builder import ContextBuilder
from automation_orchestrator.flow_builder import scenario_to_flow
from automation_orchestrator.flow_compiler import compile_flow
from automation_orchestrator.flow_validation import validate_flow
from automation_orchestrator.models import ScenarioManifest, StepError, StepResult, TriggerEvent
from automation_orchestrator.sandbox_manager import SandboxManager
from automation_orchestrator.scenario_registry import ScenarioRegistry
from automation_orchestrator.service import AgentService
from automation_orchestrator.worker import process_one, reconcile_workflows
from automation_orchestrator.workflow_engine import CommandExecutor, WorkflowEngine
from automation_orchestrator.workflow_store import WorkflowStore


def _command_flow():
    scenario = ScenarioManifest.model_validate(
        {
            "id": "graph-command",
            "version": "1",
            "title": "Graph command",
            "trigger": {"source": "manual", "event": "flow.run"},
            "start_step": "finish",
            "steps": {
                "finish": {
                    "type": "command",
                    "command": "complete",
                    "parameters": {"data": {"value": 42}},
                    "transitions": {"SUCCESS": None, "FAILURE": None},
                }
            },
        }
    )
    flow = scenario_to_flow(scenario)
    return flow.model_copy(
        update={
            "builtin": False,
            "read_only": False,
            "status": "draft",
            "nodes": [node.model_copy(update={"read_only": False}) for node in flow.nodes],
        }
    )


def _event_command_flow(flow_id: str, source: str, event: str):
    flow = _command_flow()
    return flow.model_copy(
        update={
            "id": flow_id,
            "title": f"Event flow {flow_id}",
            "nodes": [
                node.model_copy(
                    update={
                        "config": {"source": source, "event": event},
                        "subtitle": f"{source} · {event}",
                    }
                )
                if node.type == "trigger"
                else node
                for node in flow.nodes
            ],
        }
    )


def _review_flow():
    scenario = ScenarioManifest.model_validate(
        {
            "id": "graph-review",
            "version": "1",
            "title": "Graph review",
            "trigger": {"source": "manual", "event": "flow.run"},
            "start_step": "approval",
            "steps": {
                "approval": {
                    "type": "review",
                    "provider": "gitea",
                    "decision": "review",
                    "input_mapping": {"flow_id": "${{ trigger.flow.id }}"},
                    "transitions": {"SUCCESS": None, "FAILURE": None},
                }
            },
        }
    )
    flow = scenario_to_flow(scenario)
    return flow.model_copy(
        update={
            "builtin": False,
            "read_only": False,
            "status": "draft",
            "nodes": [node.model_copy(update={"read_only": False}) for node in flow.nodes],
        }
    )


def _retry_flow():
    scenario = ScenarioManifest.model_validate(
        {
            "id": "graph-retry",
            "version": "1",
            "title": "Graph retry",
            "trigger": {"source": "manual", "event": "flow.run"},
            "start_step": "unstable",
            "steps": {
                "unstable": {
                    "type": "command",
                    "command": "complete",
                    "input_mapping": {"data.value": "${{ inputs.payload.value }}"},
                    "retry": {
                        "max_attempts": 2,
                        "delay_seconds": 0,
                        "max_delay_seconds": 0,
                    },
                    "transitions": {"SUCCESS": None, "FAILURE": None},
                }
            },
        }
    )
    flow = scenario_to_flow(scenario)
    return flow.model_copy(
        update={
            "builtin": False,
            "read_only": False,
            "status": "draft",
            "nodes": [node.model_copy(update={"read_only": False}) for node in flow.nodes],
        }
    )


def _branch_flow():
    scenario = ScenarioManifest.model_validate(
        {
            "id": "graph-branch",
            "version": "1",
            "title": "Graph branch",
            "trigger": {"source": "manual", "event": "flow.run"},
            "start_step": "choose",
            "steps": {
                "choose": {
                    "type": "command",
                    "command": "complete",
                    "transitions": {"SUCCESS": "success-path", "FAILURE": "failure-path"},
                },
                "success-path": {
                    "type": "command",
                    "command": "complete",
                    "transitions": {"SUCCESS": None, "FAILURE": None},
                },
                "failure-path": {
                    "type": "command",
                    "command": "fail",
                    "transitions": {"SUCCESS": None, "FAILURE": None},
                },
            },
        }
    )
    flow = scenario_to_flow(scenario)
    return flow.model_copy(
        update={
            "builtin": False,
            "read_only": False,
            "status": "draft",
            "nodes": [node.model_copy(update={"read_only": False}) for node in flow.nodes],
        }
    )


def _binding_flow():
    scenario = ScenarioManifest.model_validate(
        {
            "id": "graph-bindings",
            "version": "1",
            "title": "Graph bindings",
            "trigger": {"source": "manual", "event": "flow.run"},
            "start_step": "produce",
            "steps": {
                "produce": {
                    "type": "command",
                    "command": "complete",
                    "parameters": {"data": {"static": "kept"}},
                    "input_mapping": {"data.value": "${{ inputs.payload.value }}"},
                    "transitions": {"SUCCESS": "consume", "FAILURE": None},
                },
                "consume": {
                    "type": "command",
                    "command": "complete",
                    "input_mapping": {"data": "${{ nodes.produce.data }}"},
                    "transitions": {"SUCCESS": None, "FAILURE": None},
                },
            },
        }
    )
    flow = scenario_to_flow(scenario)
    return flow.model_copy(
        update={
            "builtin": False,
            "read_only": False,
            "status": "draft",
            "nodes": [node.model_copy(update={"read_only": False}) for node in flow.nodes],
        }
    )


def _control_flow(flow_id: str, step: dict):
    scenario = ScenarioManifest.model_validate(
        {
            "id": flow_id,
            "version": "1",
            "title": flow_id,
            "trigger": {"source": "manual", "event": "flow.run"},
            "start_step": "control",
            "steps": {
                "control": {
                    **step,
                    "transitions": {"SUCCESS": None, "FAILURE": None},
                }
            },
        }
    )
    flow = scenario_to_flow(scenario)
    return flow.model_copy(
        update={
            "builtin": False,
            "read_only": False,
            "status": "draft",
            "nodes": [node.model_copy(update={"read_only": False}) for node in flow.nodes],
        }
    )


class RetryOnceExecutor(CommandExecutor):
    def execute(self, *, workflow, step_id, iteration, attempt, step):
        if attempt == 1:
            return StepResult(
                step_id=step_id,
                execution_id=self.execution_id(workflow.id, step_id, iteration, attempt),
                iteration=iteration,
                attempt=attempt,
                execution_status="ERROR",
                outcome=None,
                error=StepError(code="TRANSIENT", message="retry", retryable=True),
            )
        return super().execute(
            workflow=workflow,
            step_id=step_id,
            iteration=iteration,
            attempt=attempt,
            step=step,
        )


class AlwaysFailExecutor(CommandExecutor):
    def execute(self, *, workflow, step_id, iteration, attempt, step):
        return StepResult(
            step_id=step_id,
            execution_id=self.execution_id(workflow.id, step_id, iteration, attempt),
            iteration=iteration,
            attempt=attempt,
            execution_status="ERROR",
            outcome=None,
            error=StepError(code="PERSISTENT", message="manual retry required", retryable=True),
        )


def _service(image_resolver, tmp_path: Path, *, command_executor=None):
    service = AgentService(
        ContextBuilder(), image_resolver, SandboxManager(tmp_path / "jobs")
    )
    service.scenario_registry = ScenarioRegistry(Path(__file__).parents[1] / "scenarios")
    service.workflow_engine = WorkflowEngine(
        service.scenario_registry,
        WorkflowStore(tmp_path / "workflows"),
        service,
        command_executor=command_executor,
    )
    client = TestClient(create_app(service))
    return service, client


def test_published_graph_runs_through_existing_command_executor(
    image_resolver, tmp_path: Path
):
    service, client = _service(image_resolver, tmp_path)
    draft = service.flow_store.create(_command_flow())
    published = service.flow_store.publish(draft.id, expected_revision=draft.revision)

    compiled = compile_flow(published.definition)
    assert compiled.steps["finish"].command == "complete"

    response = client.post(
        f"/v1/flows/{draft.id}/runs",
        json={"version": 1, "inputs": {"request": "smoke"}},
    )
    assert response.status_code == 202
    created = response.json()
    assert created["status"] == "CREATED"
    assert created["activated_edges"][0]["source"] == "__trigger__"
    assert service.workflow_queue.get(created["id"]).status == "PENDING"

    assert process_one(service, worker_id="graph-worker", heartbeat_seconds=0.01)
    completed = client.get(f"/v1/runs/{created['id']}").json()

    assert completed["status"] == "COMPLETED"
    assert completed["outcome"] == "SUCCESS"
    assert completed["current_node"] == "__success__"
    assert completed["node_runs"][0]["node_id"] == "finish"
    assert completed["node_runs"][0]["data"]["value"] == 42
    assert [edge["source"] for edge in completed["activated_edges"]] == [
        "__trigger__",
        "finish",
    ]


def test_event_endpoint_fans_out_to_published_flows_idempotently(
    image_resolver, tmp_path: Path
):
    service, client = _service(image_resolver, tmp_path)
    for flow_id in ("orders-primary", "orders-audit"):
        draft = service.flow_store.create(
            _event_command_flow(flow_id, "webhook", "webhook.received")
        )
        service.flow_store.publish(draft.id, expected_revision=draft.revision)

    payload = {
        "source": "webhook",
        "event": "webhook.received",
        "event_id": "order-event-1",
        "data": {"order": {"id": "A-42"}},
    }
    first = client.post("/v1/events", json=payload)
    duplicate = client.post("/v1/events", json=payload)

    assert first.status_code == 202
    assert first.json()["accepted"] is True
    assert {run["flow_id"] for run in first.json()["flow_runs"]} == {
        "orders-primary",
        "orders-audit",
    }
    assert [run["id"] for run in duplicate.json()["flow_runs"]] == [
        run["id"] for run in first.json()["flow_runs"]
    ]
    assert service.workflow_queue.summary()["pending"] == 2
    workflow = service.workflow_engine.get(first.json()["flow_runs"][0]["id"])
    assert workflow.trigger.source == "webhook"
    assert workflow.trigger.event == "webhook.received"
    assert workflow.trigger.data["order"]["id"] == "A-42"

    unmatched = client.post(
        "/v1/events",
        json={**payload, "event": "unsupported", "event_id": "order-event-2"},
    )
    assert unmatched.status_code == 202
    assert unmatched.json()["accepted"] is False
    assert unmatched.json()["flow_runs"] == []


def test_signed_gitea_push_starts_matching_flow_and_suppresses_automation_branch(
    image_resolver, tmp_path: Path, monkeypatch
):
    service, client = _service(image_resolver, tmp_path)
    draft = service.flow_store.create(_event_command_flow("gitea-push", "gitea", "push"))
    service.flow_store.publish(draft.id, expected_revision=draft.revision)
    secret = "gitea-flow-secret"
    monkeypatch.setenv("GITEA_WEBHOOK_SECRET", secret)
    payload = {
        "ref": "refs/heads/main",
        "after": "b" * 40,
        "repository": {"full_name": "team/service"},
        "commits": [{"id": "b" * 40, "message": "trigger flow"}],
    }

    def send(delivery: str, body_payload: dict):
        body = json.dumps(body_payload, separators=(",", ":")).encode()
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return client.post(
            "/v1/webhooks/gitea",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Gitea-Event-Type": "push",
                "X-Gitea-Delivery": delivery,
                "X-Gitea-Signature": signature,
            },
        )

    first = send("gitea-flow-delivery", payload)
    duplicate = send("gitea-flow-delivery", payload)
    suppressed = send(
        "gitea-automation-delivery",
        {**payload, "ref": "refs/heads/automation/flow-run-test"},
    )

    assert first.status_code == 202
    assert first.json()["accepted"] is True
    assert first.json()["flow_runs"][0]["flow_id"] == "gitea-push"
    assert duplicate.json()["flow_runs"][0]["id"] == first.json()["flow_runs"][0]["id"]
    assert suppressed.json() == {
        "accepted": False,
        "reason": "push workflows are disabled",
    }


def test_signed_plane_event_starts_matching_flow(image_resolver, tmp_path: Path, monkeypatch):
    service, client = _service(image_resolver, tmp_path)
    draft = service.flow_store.create(
        _event_command_flow("plane-ready", "plane", "issue.ready_for_development")
    )
    service.flow_store.publish(draft.id, expected_revision=draft.revision)
    secret = "plane-flow-secret"
    monkeypatch.setenv("PLANE_WEBHOOK_SECRET", secret)
    monkeypatch.setenv("PLANE_READY_STATE_NAMES", "Ready for development")
    monkeypatch.setenv("PLANE_PROJECT_REPOSITORIES", '{"PAY":"team/service"}')
    payload = {
        "event": "issue",
        "action": "updated",
        "webhook_id": "plane-flow-webhook",
        "data": {
            "id": "issue-flow-1",
            "name": "Run custom flow",
            "updated_at": "2026-08-27T09:30:00Z",
            "project": {"id": "project-1", "identifier": "PAY"},
            "state_detail": {"id": "state-ready", "name": "Ready for development"},
        },
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Plane-Event": "issue",
        "X-Plane-Delivery": "plane-flow-delivery",
        "X-Plane-Signature": signature,
    }

    first = client.post("/v1/webhooks/plane", content=body, headers=headers)
    duplicate = client.post("/v1/webhooks/plane", content=body, headers=headers)

    assert first.status_code == 202
    assert first.json()["accepted"] is True
    assert first.json()["flow_runs"][0]["flow_id"] == "plane-ready"
    assert duplicate.json()["flow_runs"][0]["id"] == first.json()["flow_runs"][0]["id"]


def test_graph_runtime_resolves_trigger_and_upstream_node_bindings(
    image_resolver, tmp_path: Path
):
    service, client = _service(image_resolver, tmp_path)
    draft = service.flow_store.create(_binding_flow())
    published = service.flow_store.publish(draft.id, expected_revision=draft.revision)

    compiled = compile_flow(published.definition)
    assert compiled.steps["produce"].input_mapping == {
        "data.value": "${{ inputs.payload.value }}"
    }

    created = client.post(
        f"/v1/flows/{draft.id}/runs",
        json={"inputs": {"payload": {"value": 42}}},
    ).json()
    assert process_one(service, worker_id="binding-worker", heartbeat_seconds=0.01)
    assert service.workflow_queue.get(created["id"]).status == "PENDING"
    assert process_one(service, worker_id="binding-worker", heartbeat_seconds=0.01)
    completed = client.get(f"/v1/runs/{created['id']}").json()

    node_runs = {node["node_id"]: node for node in completed["node_runs"]}
    produce = node_runs["produce"]
    consume = node_runs["consume"]
    assert produce["inputs"] == {"data": {"value": 42}}
    assert produce["data"]["static"] == "kept"
    assert produce["data"]["value"] == 42
    assert consume["inputs"]["data"] == produce["data"]
    assert consume["data"]["value"] == 42


def test_graph_runtime_reports_missing_dynamic_input_as_node_error(
    image_resolver, tmp_path: Path
):
    service, client = _service(image_resolver, tmp_path)
    draft = service.flow_store.create(_binding_flow())
    service.flow_store.publish(draft.id, expected_revision=draft.revision)
    created = client.post(f"/v1/flows/{draft.id}/runs", json={}).json()

    assert process_one(service, worker_id="binding-error-worker", heartbeat_seconds=0.01)
    failed = client.get(f"/v1/runs/{created['id']}").json()

    node_runs = {node["node_id"]: node for node in failed["node_runs"]}
    assert failed["status"] == "FAILED"
    assert node_runs["produce"]["status"] == "ERROR"
    assert node_runs["produce"]["error"]["code"] == "NODE_INPUT_ERROR"


def test_if_node_selects_business_outcome_from_expression(image_resolver, tmp_path: Path):
    service, client = _service(image_resolver, tmp_path)
    draft = service.flow_store.create(
        _control_flow(
            "graph-if",
            {"type": "if", "condition": "${{ inputs.approved }}"},
        )
    )
    service.flow_store.publish(draft.id, expected_revision=draft.revision)

    approved = client.post(
        f"/v1/flows/{draft.id}/runs",
        json={"inputs": {"approved": True}},
    ).json()
    assert process_one(service, worker_id="if-worker", heartbeat_seconds=0.01)
    approved_run = client.get(f"/v1/runs/{approved['id']}").json()

    rejected = client.post(
        f"/v1/flows/{draft.id}/runs",
        json={"inputs": {"approved": False}},
    ).json()
    assert process_one(service, worker_id="if-worker", heartbeat_seconds=0.01)
    rejected_run = client.get(f"/v1/runs/{rejected['id']}").json()

    assert approved_run["outcome"] == "SUCCESS"
    assert approved_run["node_runs"][0]["data"]["condition"] is True
    assert rejected_run["outcome"] == "FAILURE"
    assert rejected_run["node_runs"][0]["data"]["condition"] is False


def test_switch_node_uses_match_and_default_outcomes(image_resolver, tmp_path: Path):
    service, client = _service(image_resolver, tmp_path)
    draft = service.flow_store.create(
        _control_flow(
            "graph-switch",
            {"type": "switch", "value": "${{ inputs.mode }}", "equals": "fast"},
        )
    )
    service.flow_store.publish(draft.id, expected_revision=draft.revision)

    created = client.post(
        f"/v1/flows/{draft.id}/runs",
        json={"inputs": {"mode": "slow"}},
    ).json()
    assert process_one(service, worker_id="switch-worker", heartbeat_seconds=0.01)
    completed = client.get(f"/v1/runs/{created['id']}").json()

    assert completed["outcome"] == "FAILURE"
    assert completed["node_runs"][0]["data"] == {
        "summary": "switch used default",
        "value": "slow",
        "equals": "fast",
        "matched": False,
    }


def test_delay_node_defers_queue_and_resumes_without_holding_worker(
    image_resolver, tmp_path: Path
):
    service, client = _service(image_resolver, tmp_path)
    draft = service.flow_store.create(
        _control_flow("graph-delay", {"type": "delay", "seconds": 0})
    )
    service.flow_store.publish(draft.id, expected_revision=draft.revision)
    created = client.post(f"/v1/flows/{draft.id}/runs", json={}).json()

    assert process_one(service, worker_id="delay-worker", heartbeat_seconds=0.01)
    waiting = client.get(f"/v1/runs/{created['id']}").json()
    assert waiting["status"] == "WAITING"
    assert waiting["node_runs"][0]["status"] == "WAITING"
    assert service.workflow_queue.get(created["id"]).status == "PENDING"

    restarted_service, restarted_client = _service(image_resolver, tmp_path)
    assert process_one(
        restarted_service,
        worker_id="delay-worker-after-restart",
        heartbeat_seconds=0.01,
    )
    completed = restarted_client.get(f"/v1/runs/{created['id']}").json()
    assert completed["status"] == "COMPLETED"
    assert completed["outcome"] == "SUCCESS"
    assert completed["node_runs"][0]["status"] == "COMPLETED"


def test_merge_any_executes_and_parallel_merge_all_is_rejected(image_resolver, tmp_path: Path):
    merge_any = _control_flow("graph-merge-any", {"type": "merge", "mode": "any"})
    assert validate_flow(merge_any).valid is True
    service, client = _service(image_resolver, tmp_path)
    draft = service.flow_store.create(merge_any)
    service.flow_store.publish(draft.id, expected_revision=draft.revision)
    created = client.post(f"/v1/flows/{draft.id}/runs", json={}).json()
    assert process_one(service, worker_id="merge-worker", heartbeat_seconds=0.01)
    completed = client.get(f"/v1/runs/{created['id']}").json()
    assert completed["status"] == "COMPLETED"
    assert completed["node_runs"][0]["data"]["summary"] == "merge.any activated"

    scenario = ScenarioManifest.model_validate(
        {
            "id": "graph-merge-all",
            "trigger": {"source": "manual", "event": "flow.run"},
            "start_step": "choose",
            "steps": {
                "choose": {
                    "type": "if",
                    "condition": "${{ inputs.first }}",
                    "transitions": {"SUCCESS": "left", "FAILURE": "right"},
                },
                "left": {
                    "type": "merge",
                    "mode": "any",
                    "transitions": {"SUCCESS": "join", "FAILURE": "join"},
                },
                "right": {
                    "type": "merge",
                    "mode": "any",
                    "transitions": {"SUCCESS": "join", "FAILURE": "join"},
                },
                "join": {
                    "type": "merge",
                    "mode": "all",
                    "transitions": {"SUCCESS": None, "FAILURE": None},
                },
            },
        }
    )
    result = validate_flow(scenario_to_flow(scenario))

    assert result.valid is False
    assert "merge-all-parallel-unavailable" in {issue.code for issue in result.errors}


def test_outbox_recovers_run_created_before_dispatch(image_resolver, tmp_path: Path):
    service, client = _service(image_resolver, tmp_path)
    draft = service.flow_store.create(_command_flow())
    published = service.flow_store.publish(draft.id, expected_revision=draft.revision)
    run_id = "flow-run-crash-recovery"
    service.graph_run_store.register(
        run_id=run_id,
        version=published,
        scenario=compile_flow(published.definition),
        inputs={"request": "recover"},
        trigger_event=TriggerEvent(
            source="manual",
            event="flow.run",
            event_id=run_id,
            data={"request": "recover"},
        ),
    )

    assert service.workflow_engine.get(run_id) is None
    reconcile_workflows(service)
    assert service.workflow_engine.get(run_id) is not None
    assert service.workflow_queue.get(run_id).status == "PENDING"
    assert service.graph_run_store.pending_outbox() == []

    assert process_one(service, worker_id="recovery-worker", heartbeat_seconds=0.01)
    assert client.get(f"/v1/runs/{run_id}").json()["status"] == "COMPLETED"


def test_node_outbox_recovers_after_result_save_before_continuation_dispatch(
    image_resolver, tmp_path: Path
):
    service, _ = _service(image_resolver, tmp_path)
    draft = service.flow_store.create(_binding_flow())
    service.flow_store.publish(draft.id, expected_revision=draft.revision)
    run = service.graph_runtime.start(
        draft.id,
        inputs={"payload": {"value": 42}},
    )
    claimed = service.workflow_queue.claim("crashed-worker", lease_seconds=30)
    assert claimed is not None

    workflow = service.workflow_engine.get(run.id)
    advanced = service.workflow_engine.advance_safely(workflow, transition_budget=1)
    service.graph_run_store.sync(advanced)
    pending = service.graph_run_store.pending_node_outbox(run_id=run.id)
    assert pending == [(f"{run.id}-produce-1-1:continue", run.id)]
    assert service.workflow_queue.complete(run.id, "crashed-worker") is True
    assert service.workflow_queue.get(run.id).status == "COMPLETED"

    restarted_service, restarted_client = _service(image_resolver, tmp_path)
    reconcile_workflows(restarted_service)

    assert restarted_service.graph_run_store.pending_node_outbox(run_id=run.id) == []
    assert restarted_service.workflow_queue.get(run.id).status == "PENDING"
    assert process_one(
        restarted_service,
        worker_id="recovered-node-worker",
        heartbeat_seconds=0.01,
    )
    completed = restarted_client.get(f"/v1/runs/{run.id}").json()
    assert completed["status"] == "COMPLETED"
    assert {node["node_id"]: node["status"] for node in completed["node_runs"]} == {
        "consume": "COMPLETED",
        "produce": "COMPLETED",
    }


def test_graph_review_waits_and_resumes_via_run_api(image_resolver, tmp_path: Path):
    service, client = _service(image_resolver, tmp_path)
    draft = service.flow_store.create(_review_flow())
    service.flow_store.publish(draft.id, expected_revision=draft.revision)
    created = client.post(f"/v1/flows/{draft.id}/runs", json={}).json()

    assert process_one(service, worker_id="review-worker", heartbeat_seconds=0.01)
    waiting = client.get(f"/v1/runs/{created['id']}").json()
    assert waiting["status"] == "WAITING"
    assert waiting["node_runs"][0]["status"] == "WAITING"
    assert waiting["node_runs"][0]["inputs"] == {"flow_id": "graph-review"}

    reviewed = client.post(
        f"/v1/runs/{created['id']}/review",
        json={"outcome": "SUCCESS", "comments": ["approved"]},
    )
    assert reviewed.status_code == 202
    assert process_one(service, worker_id="review-worker", heartbeat_seconds=0.01)
    completed = client.get(f"/v1/runs/{created['id']}").json()
    assert completed["status"] == "COMPLETED"
    assert completed["node_runs"][0]["status"] == "COMPLETED"


def test_graph_retry_persists_each_node_attempt(image_resolver, tmp_path: Path):
    service, client = _service(
        image_resolver, tmp_path, command_executor=RetryOnceExecutor()
    )
    draft = service.flow_store.create(_retry_flow())
    service.flow_store.publish(draft.id, expected_revision=draft.revision)
    created = client.post(
        f"/v1/flows/{draft.id}/runs",
        json={"inputs": {"payload": {"value": 42}}},
    ).json()

    assert process_one(service, worker_id="retry-worker", heartbeat_seconds=0.01)
    assert service.workflow_queue.get(created["id"]).status == "PENDING"
    workflow = service.workflow_engine.get(created["id"])
    workflow.trigger.data["payload"]["value"] = 99
    service.workflow_engine.store.save(workflow)
    assert process_one(service, worker_id="retry-worker", heartbeat_seconds=0.01)
    completed = client.get(f"/v1/runs/{created['id']}").json()

    assert completed["status"] == "COMPLETED"
    assert [(run["attempt"], run["status"]) for run in completed["node_runs"]] == [
        (1, "ERROR"),
        (2, "COMPLETED"),
    ]
    assert [run["inputs"] for run in completed["node_runs"]] == [
        {"data": {"value": 42}},
        {"data": {"value": 42}},
    ]
    assert completed["node_runs"][-1]["data"]["value"] == 42


def test_graph_projects_node_readiness_and_marks_inactive_branch_skipped(
    image_resolver, tmp_path: Path
):
    service, client = _service(image_resolver, tmp_path)
    draft = service.flow_store.create(_branch_flow())
    service.flow_store.publish(draft.id, expected_revision=draft.revision)

    created = client.post(f"/v1/flows/{draft.id}/runs", json={}).json()
    initial = {run["node_id"]: run["status"] for run in created["node_runs"]}

    assert initial == {
        "choose": "READY",
        "failure-path": "PENDING",
        "success-path": "PENDING",
    }

    assert process_one(service, worker_id="branch-worker", heartbeat_seconds=0.01)
    assert service.workflow_queue.get(created["id"]).status == "PENDING"
    assert process_one(service, worker_id="branch-worker", heartbeat_seconds=0.01)
    completed = client.get(f"/v1/runs/{created['id']}").json()
    final = {run["node_id"]: run["status"] for run in completed["node_runs"]}

    assert completed["status"] == "COMPLETED"
    assert final == {
        "choose": "COMPLETED",
        "failure-path": "SKIPPED",
        "success-path": "COMPLETED",
    }


def test_manual_node_retry_only_requeues_the_current_failed_node(
    image_resolver, tmp_path: Path
):
    service, client = _service(
        image_resolver, tmp_path, command_executor=AlwaysFailExecutor()
    )
    draft = service.flow_store.create(_command_flow())
    service.flow_store.publish(draft.id, expected_revision=draft.revision)
    created = client.post(f"/v1/flows/{draft.id}/runs", json={}).json()

    assert process_one(service, worker_id="failing-worker", heartbeat_seconds=0.01)
    failed = client.get(f"/v1/runs/{created['id']}").json()
    assert failed["status"] == "FAILED"

    wrong = client.post(
        f"/v1/runs/{created['id']}/nodes/other/retry",
        json={"reason": "wrong node"},
    )
    assert wrong.status_code == 409

    service.workflow_engine.command_executor = CommandExecutor()
    retried = client.post(
        f"/v1/runs/{created['id']}/nodes/finish/retry",
        json={"reason": "operator approved retry"},
    )
    assert retried.status_code == 202
    assert service.workflow_queue.get(created["id"]).status == "PENDING"
    assert process_one(service, worker_id="recovery-worker", heartbeat_seconds=0.01)

    completed = client.get(f"/v1/runs/{created['id']}").json()
    assert completed["status"] == "COMPLETED"
    assert [(run["iteration"], run["status"]) for run in completed["node_runs"]] == [
        (1, "ERROR"),
        (2, "COMPLETED"),
    ]
