from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .audit_store import AuditStore
from .bootstrap import build_service
from .capability_registry import CapabilityResolutionError
from .image_builder import ImageBuildError
from .image_registry import ImageResolutionError
from .job_store import IdempotencyConflict
from .models import (
    IDENTIFIER_PATTERN,
    AgentRunRequest,
    ArtifactCleanupResult,
    ArtifactRecord,
    AuditEvent,
    BuiltContext,
    CapabilityManifest,
    ImageProfileManifest,
    PluginManifest,
    PreparedAgentStep,
    ReviewDecision,
    ScenarioManifest,
    SkillManifest,
    StepResult,
    TriggerEvent,
    WorkflowActionRequest,
    WorkflowInstance,
)
from .plane_webhook import (
    PlaneWebhookError,
    normalize_plane_webhook,
    parse_csv,
    parse_project_repositories,
)
from .plugin_registry import PluginResolutionError
from .sandbox_manager import SandboxExecutionError
from .scenario_registry import ScenarioResolutionError
from .service import AgentService
from .skill_registry import SkillResolutionError
from .workflow_engine import WorkflowExecutionError
from .workflow_queue import WorkflowQueue

WORKFLOW_MARKER = re.compile(r"<!--\s*automation-workflow:\s*([A-Za-z0-9._-]+)\s*-->")
GITEA_REVIEW_OUTCOMES = {
    "pull_request_review_approved": "SUCCESS",
    "pull_request_approved": "SUCCESS",
    "pull_request_review_rejected": "FAILURE",
    "pull_request_rejected": "FAILURE",
    "pull_request_review_comment": "FAILURE",
    "pull_request_comment": "FAILURE",
}
GITEA_PULL_EVENTS = {
    "pull_request",
    "pull_request_sync",
    "pull_request_review_request",
}


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "")) or "unknown"


def _audit_request(
    request: Request,
    *,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    outcome: str = "SUCCESS",
    details: dict[str, Any] | None = None,
) -> None:
    client = request.client.host if request.client else None
    request.app.state.service.audit_store.record(
        actor=str(getattr(request.state, "actor", "local-client")),
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        request_id=_request_id(request),
        source_ip=client,
        details=details,
    )


def _workflow_marker(payload: dict[str, Any]) -> str | None:
    pull = payload.get("pull_request")
    if not isinstance(pull, dict) or not isinstance(pull.get("body"), str):
        return None
    match = WORKFLOW_MARKER.search(pull["body"])
    if match is None or not IDENTIFIER_PATTERN.fullmatch(match.group(1)):
        return None
    return match.group(1)


def _review_comments(payload: dict[str, Any]) -> list[str]:
    comments: list[str] = []
    for key in ("review", "comment"):
        item = payload.get(key)
        body = item.get("body") if isinstance(item, dict) else None
        if isinstance(body, str) and body.strip():
            comments.append(body.strip()[:4000])
    return comments


def create_app(service: AgentService | None = None) -> FastAPI:
    application = FastAPI(title="Automation Orchestrator", version="0.1.0")
    dashboard_origins = [
        origin.strip()
        for origin in os.environ.get(
            "DASHBOARD_ORIGINS",
            "http://127.0.0.1:4173,http://localhost:4173",
        ).split(",")
        if origin.strip()
    ]
    application.add_middleware(
        CORSMiddleware,
        allow_origins=dashboard_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    current_service = service or build_service()
    if not hasattr(current_service, "workflow_queue"):
        current_service.workflow_queue = WorkflowQueue(
            current_service.sandbox_manager.jobs_root / "workflow-queue.sqlite3"
        )
    if not hasattr(current_service, "audit_store"):
        current_service.audit_store = AuditStore(
            current_service.sandbox_manager.jobs_root / "audit.sqlite3"
        )
    workflow_engine = getattr(current_service, "workflow_engine", None)
    if workflow_engine is not None and getattr(workflow_engine, "audit_store", None) is None:
        workflow_engine.audit_store = current_service.audit_store
    application.state.service = current_service

    @application.middleware("http")
    async def identify_request(request: Request, call_next):
        request_id = request.headers.get("x-request-id", "").strip()[:200] or str(uuid4())
        request.state.request_id = request_id
        webhook_actors = {
            "/v1/webhooks/gitea": "gitea-webhook",
            "/v1/webhooks/plane": "plane-webhook",
        }
        request.state.actor = webhook_actors.get(request.url.path, "local-client")
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @application.get("/health")
    def health(request: Request) -> dict[str, object]:
        current: AgentService = request.app.state.service
        return {
            "status": "ok",
            "docker": current.sandbox_manager.is_available(),
            "providers": {
                "openai": {
                    "configured": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
                },
                "openrouter": {
                    "configured": bool(os.environ.get("OPENROUTER_API_KEY", "").strip()),
                },
                "gitea": {
                    "configured": bool(os.environ.get("GITEA_TOKEN", "").strip()),
                    "url": os.environ.get("GITEA_BASE_URL", "http://gitea:3000"),
                },
                "plane": {
                    "configured": bool(
                        os.environ.get("PLANE_WEBHOOK_SECRET", "").strip()
                        and os.environ.get("PLANE_PROJECT_REPOSITORIES", "").strip()
                    ),
                    "url": os.environ.get("PLANE_BASE_URL", ""),
                },
                "swirl": {
                    "configured": getattr(
                        getattr(current, "workflow_engine", None), "swirl_client", None
                    )
                    is not None,
                    "url": os.environ.get("SWIRL_BASE_URL", ""),
                },
            },
            "default_agent": {
                "provider": os.environ.get("DEFAULT_AGENT_PROVIDER", "openrouter"),
                "model": os.environ.get("DEFAULT_AGENT_MODEL", "openai/gpt-4.1-nano"),
            },
            "queue": current.workflow_queue.summary(),
        }

    @application.get("/v1/audit-events", response_model=list[AuditEvent])
    def audit_events(
        request: Request,
        limit: int = 100,
        before_id: int | None = None,
        action: str | None = None,
        resource_id: str | None = None,
    ) -> list[AuditEvent]:
        if limit < 1 or limit > 500:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
        return request.app.state.service.audit_store.list(
            limit=limit,
            before_id=before_id,
            action=action,
            resource_id=resource_id,
        )

    @application.get("/v1/plugins", response_model=list[PluginManifest])
    def plugins(request: Request) -> list[PluginManifest]:
        return request.app.state.service.plugin_registry.list()

    @application.get("/v1/skills", response_model=list[SkillManifest])
    def skills(request: Request) -> list[SkillManifest]:
        return request.app.state.service.skill_registry.list()

    @application.get("/v1/capabilities", response_model=list[CapabilityManifest])
    def capabilities(request: Request) -> list[CapabilityManifest]:
        return request.app.state.service.capability_registry.list()

    @application.get("/v1/images", response_model=list[ImageProfileManifest])
    def images(request: Request) -> list[ImageProfileManifest]:
        return request.app.state.service.image_registry.list()

    @application.get("/v1/scenarios", response_model=list[ScenarioManifest])
    def scenarios(request: Request) -> list[ScenarioManifest]:
        registry = getattr(request.app.state.service, "scenario_registry", None)
        if registry is None:
            raise HTTPException(status_code=503, detail="workflow engine is not configured")
        return registry.list()

    @application.post(
        "/v1/triggers",
        response_model=WorkflowInstance,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def receive_trigger(payload: TriggerEvent, request: Request) -> WorkflowInstance:
        engine = getattr(request.app.state.service, "workflow_engine", None)
        if engine is None:
            raise HTTPException(status_code=503, detail="workflow engine is not configured")
        try:
            workflow, created = engine.create(payload)
            enqueued = False
            if workflow.status in {"CREATED", "RUNNING"}:
                enqueued = request.app.state.service.workflow_queue.enqueue(workflow.id)
            _audit_request(
                request,
                action="workflow.trigger.received",
                resource_type="workflow",
                resource_id=workflow.id,
                details={
                    "source": payload.source,
                    "event": payload.event,
                    "event_id": payload.event_id,
                    "created": created,
                    "enqueued": enqueued,
                },
            )
            return workflow
        except ScenarioResolutionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except WorkflowExecutionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.post(
        "/v1/webhooks/plane",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def receive_plane_webhook(request: Request) -> dict[str, Any]:
        secret = os.environ.get("PLANE_WEBHOOK_SECRET", "").strip()
        if not secret:
            raise HTTPException(status_code=503, detail="Plane webhook is not configured")

        body = await request.body()
        if len(body) > 2_000_000:
            raise HTTPException(status_code=413, detail="webhook payload is too large")
        signature = request.headers.get("x-plane-signature", "").strip()
        expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        if not signature or not hmac.compare_digest(expected, signature):
            _audit_request(
                request,
                action="plane.webhook.rejected",
                resource_type="webhook",
                outcome="DENIED",
                details={"reason": "invalid signature"},
            )
            raise HTTPException(status_code=401, detail="invalid Plane signature")

        try:
            payload: Any = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail="invalid JSON payload") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="webhook payload must be an object")

        header_event = request.headers.get("x-plane-event", "").strip().casefold()
        payload_event = str(payload.get("event", "")).strip().casefold()
        if header_event and header_event != payload_event:
            raise HTTPException(status_code=400, detail="Plane event header does not match payload")

        try:
            normalized = normalize_plane_webhook(
                payload,
                delivery=request.headers.get("x-plane-delivery", "").strip()[:200] or None,
                repositories=parse_project_repositories(
                    os.environ.get("PLANE_PROJECT_REPOSITORIES", "")
                ),
                ready_state_ids=parse_csv(os.environ.get("PLANE_READY_STATE_IDS", "")),
                ready_state_names=parse_csv(os.environ.get("PLANE_READY_STATE_NAMES", "")),
            )
        except PlaneWebhookError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        if normalized.trigger is None:
            _audit_request(
                request,
                action="plane.webhook.ignored",
                resource_type="webhook",
                details={"event": payload_event, "reason": normalized.reason},
            )
            return {"accepted": False, "reason": normalized.reason}

        engine = getattr(request.app.state.service, "workflow_engine", None)
        if engine is None:
            raise HTTPException(status_code=503, detail="workflow engine is not configured")
        try:
            workflow, created = engine.create(normalized.trigger)
            enqueued = False
            if workflow.status in {"CREATED", "RUNNING"}:
                enqueued = request.app.state.service.workflow_queue.enqueue(workflow.id)
            _audit_request(
                request,
                action="plane.webhook.accepted",
                resource_type="workflow",
                resource_id=workflow.id,
                details={
                    "event_id": normalized.trigger.event_id,
                    "created": created,
                    "enqueued": enqueued,
                },
            )
            return {
                "accepted": True,
                "created": created,
                "enqueued": enqueued,
                "workflow": workflow.model_dump(mode="json"),
            }
        except ScenarioResolutionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except WorkflowExecutionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.post(
        "/v1/webhooks/gitea",
        response_model=WorkflowInstance,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def receive_gitea_webhook(request: Request) -> WorkflowInstance:
        secret = os.environ.get("GITEA_WEBHOOK_SECRET", "").strip()
        if not secret:
            raise HTTPException(status_code=503, detail="Gitea webhook is not configured")

        body = await request.body()
        if len(body) > 2_000_000:
            raise HTTPException(status_code=413, detail="webhook payload is too large")
        signature = request.headers.get("x-gitea-signature", "")
        expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        if not signature or not hmac.compare_digest(expected, signature):
            _audit_request(
                request,
                action="gitea.webhook.rejected",
                resource_type="webhook",
                outcome="DENIED",
                details={"reason": "invalid signature"},
            )
            raise HTTPException(status_code=401, detail="invalid Gitea signature")

        try:
            payload: Any = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail="invalid JSON payload") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="webhook payload must be an object")

        event_type = (
            request.headers.get("x-gitea-event-type") or request.headers.get("x-gitea-event") or ""
        ).strip()
        if not event_type:
            raise HTTPException(status_code=400, detail="missing Gitea event type")
        delivery = request.headers.get("x-gitea-delivery", "").strip()
        if not delivery or not IDENTIFIER_PATTERN.fullmatch(delivery):
            delivery_source = delivery or expected
            delivery = f"gitea-{hashlib.sha256(delivery_source.encode()).hexdigest()[:24]}"

        engine = getattr(request.app.state.service, "workflow_engine", None)
        if engine is None:
            raise HTTPException(status_code=503, detail="workflow engine is not configured")
        if event_type in GITEA_REVIEW_OUTCOMES:
            workflow_id = _workflow_marker(payload)
            if workflow_id is None:
                raise HTTPException(status_code=422, detail="Gitea review has no workflow marker")
            workflow = engine.get(workflow_id)
            if workflow is None:
                raise HTTPException(status_code=404, detail="workflow not found")
            comment = payload.get("comment")
            comment_body = comment.get("body") if isinstance(comment, dict) else None
            if (
                event_type == "pull_request_comment"
                and isinstance(comment_body, str)
                and "<!-- automation-idempotency-key:" in comment_body
            ):
                return workflow
            pull = payload.get("pull_request")
            repository = payload.get("repository")
            full_name = repository.get("full_name") if isinstance(repository, dict) else None
            pull_index = None
            if isinstance(pull, dict):
                candidate = pull.get("number") or pull.get("index")
                pull_index = candidate if isinstance(candidate, int) else None
            pending = workflow.pending_review
            if pending is not None:
                if pending.repository and pending.repository != full_name:
                    raise HTTPException(status_code=409, detail="review repository does not match")
                if pending.pull_index and pending.pull_index != pull_index:
                    raise HTTPException(
                        status_code=409, detail="review pull request does not match"
                    )
            try:
                workflow = engine.review(
                    workflow_id,
                    ReviewDecision(
                        outcome=GITEA_REVIEW_OUTCOMES[event_type],
                        comments=_review_comments(payload),
                        external_event_id=delivery,
                        external_url=pull.get("html_url") if isinstance(pull, dict) else None,
                    ),
                    advance=False,
                )
                request.app.state.service.workflow_queue.enqueue(
                    workflow.id,
                    requeue_if_running=True,
                )
                _audit_request(
                    request,
                    action="workflow.review.received",
                    resource_type="workflow",
                    resource_id=workflow.id,
                    details={
                        "event_type": event_type,
                        "delivery": delivery,
                        "outcome": GITEA_REVIEW_OUTCOMES[event_type],
                    },
                )
                return workflow
            except WorkflowExecutionError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        if event_type in GITEA_PULL_EVENTS:
            workflow_id = _workflow_marker(payload)
            if workflow_id is None:
                raise HTTPException(
                    status_code=422, detail="Gitea pull request has no workflow marker"
                )
            workflow = engine.get(workflow_id)
            if workflow is None:
                raise HTTPException(status_code=404, detail="workflow not found")
            return workflow

        repository = payload.get("repository")
        pusher = payload.get("pusher") or payload.get("sender")
        commits = payload.get("commits")
        normalized = {
            "ref": payload.get("ref"),
            "before": payload.get("before"),
            "after": payload.get("after"),
            "compare_url": payload.get("compare_url"),
            "repository": {
                "full_name": repository.get("full_name"),
                "html_url": repository.get("html_url"),
                "clone_url": repository.get("clone_url"),
                "default_branch": repository.get("default_branch"),
            }
            if isinstance(repository, dict)
            else {},
            "sender": {
                "username": pusher.get("username") or pusher.get("login"),
                "full_name": pusher.get("full_name"),
            }
            if isinstance(pusher, dict)
            else {},
            "commits": [
                {
                    "id": commit.get("id"),
                    "message": commit.get("message"),
                    "url": commit.get("url"),
                    "author": commit.get("author"),
                }
                for commit in commits[:50]
                if isinstance(commit, dict)
            ]
            if isinstance(commits, list)
            else [],
        }
        repository_name = normalized["repository"].get("full_name")
        ref = normalized["ref"]
        if not isinstance(repository_name, str) or not repository_name.strip():
            raise HTTPException(status_code=422, detail="Gitea push has no repository full_name")
        if not isinstance(ref, str) or not ref.strip():
            raise HTTPException(status_code=422, detail="Gitea push has no ref")
        try:
            workflow, created = engine.create(
                TriggerEvent(
                    source="gitea",
                    event=event_type,
                    event_id=delivery,
                    data=normalized,
                )
            )
            enqueued = request.app.state.service.workflow_queue.enqueue(workflow.id)
            _audit_request(
                request,
                action="gitea.webhook.accepted",
                resource_type="workflow",
                resource_id=workflow.id,
                details={
                    "event_type": event_type,
                    "delivery": delivery,
                    "created": created,
                    "enqueued": enqueued,
                },
            )
            return workflow
        except ScenarioResolutionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except WorkflowExecutionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.get("/v1/workflows", response_model=list[WorkflowInstance])
    def workflows(request: Request) -> list[WorkflowInstance]:
        engine = getattr(request.app.state.service, "workflow_engine", None)
        if engine is None:
            raise HTTPException(status_code=503, detail="workflow engine is not configured")
        return engine.store.list()

    @application.get("/v1/workflows/{workflow_id}", response_model=WorkflowInstance)
    def get_workflow(workflow_id: str, request: Request) -> WorkflowInstance:
        if not IDENTIFIER_PATTERN.fullmatch(workflow_id):
            raise HTTPException(status_code=400, detail="invalid workflow_id")
        engine = getattr(request.app.state.service, "workflow_engine", None)
        if engine is None:
            raise HTTPException(status_code=503, detail="workflow engine is not configured")
        workflow = engine.get(workflow_id)
        if workflow is None:
            raise HTTPException(status_code=404, detail="workflow not found")
        return workflow

    @application.post(
        "/v1/workflows/{workflow_id}/review",
        response_model=WorkflowInstance,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def complete_review(
        workflow_id: str, payload: ReviewDecision, request: Request
    ) -> WorkflowInstance:
        if not IDENTIFIER_PATTERN.fullmatch(workflow_id):
            raise HTTPException(status_code=400, detail="invalid workflow_id")
        engine = getattr(request.app.state.service, "workflow_engine", None)
        if engine is None:
            raise HTTPException(status_code=503, detail="workflow engine is not configured")
        try:
            workflow = engine.review(workflow_id, payload, advance=False)
            request.app.state.service.workflow_queue.enqueue(
                workflow.id,
                requeue_if_running=True,
            )
            _audit_request(
                request,
                action="workflow.review.completed",
                resource_type="workflow",
                resource_id=workflow.id,
                details={"outcome": payload.outcome, "comment_count": len(payload.comments)},
            )
            return workflow
        except WorkflowExecutionError as exc:
            message = str(exc)
            status = 404 if message.startswith("unknown workflow") else 409
            raise HTTPException(status_code=status, detail=message) from exc

    @application.post(
        "/v1/workflows/{workflow_id}/cancel",
        response_model=WorkflowInstance,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def cancel_workflow(
        workflow_id: str, payload: WorkflowActionRequest, request: Request
    ) -> WorkflowInstance:
        if not IDENTIFIER_PATTERN.fullmatch(workflow_id):
            raise HTTPException(status_code=400, detail="invalid workflow_id")
        engine = getattr(request.app.state.service, "workflow_engine", None)
        if engine is None:
            raise HTTPException(status_code=503, detail="workflow engine is not configured")
        try:
            workflow = engine.cancel(workflow_id, reason=payload.reason)
            request.app.state.service.workflow_queue.cancel_pending(workflow.id)
            _audit_request(
                request,
                action="workflow.cancelled",
                resource_type="workflow",
                resource_id=workflow.id,
                details={"reason": payload.reason},
            )
            return workflow
        except WorkflowExecutionError as exc:
            message = str(exc)
            status_code = 404 if message.startswith("unknown workflow") else 409
            raise HTTPException(status_code=status_code, detail=message) from exc

    @application.post(
        "/v1/workflows/{workflow_id}/retry",
        response_model=WorkflowInstance,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def retry_workflow(
        workflow_id: str, payload: WorkflowActionRequest, request: Request
    ) -> WorkflowInstance:
        if not IDENTIFIER_PATTERN.fullmatch(workflow_id):
            raise HTTPException(status_code=400, detail="invalid workflow_id")
        engine = getattr(request.app.state.service, "workflow_engine", None)
        if engine is None:
            raise HTTPException(status_code=503, detail="workflow engine is not configured")
        try:
            queue_job = request.app.state.service.workflow_queue.get(workflow_id)
            if queue_job is not None and queue_job.status in {"PENDING", "RUNNING"}:
                raise WorkflowExecutionError("workflow already has active queue work")
            workflow = engine.retry(workflow_id, reason=payload.reason)
            enqueued = request.app.state.service.workflow_queue.enqueue(workflow.id)
            if not enqueued:
                raise WorkflowExecutionError("workflow queue refused the retry")
            _audit_request(
                request,
                action="workflow.retry.requested",
                resource_type="workflow",
                resource_id=workflow.id,
                details={"reason": payload.reason},
            )
            return workflow
        except WorkflowExecutionError as exc:
            message = str(exc)
            status_code = 404 if message.startswith("unknown workflow") else 409
            raise HTTPException(status_code=status_code, detail=message) from exc

    @application.post("/v1/context/build", response_model=BuiltContext)
    def build_context(payload: AgentRunRequest, request: Request) -> BuiltContext:
        return request.app.state.service.context_builder.build(payload.step, payload.context)

    @application.post("/v1/agent-steps/prepare", response_model=PreparedAgentStep)
    def prepare_agent_step(payload: AgentRunRequest, request: Request) -> PreparedAgentStep:
        try:
            return request.app.state.service.prepare(payload)
        except (
            PluginResolutionError,
            SkillResolutionError,
            CapabilityResolutionError,
            ImageResolutionError,
        ) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post("/v1/agent-steps/run", response_model=StepResult)
    def run_agent_step(payload: AgentRunRequest, request: Request) -> StepResult:
        try:
            result = request.app.state.service.run(payload)
            _audit_request(
                request,
                action="agent.execution.completed",
                resource_type="execution",
                resource_id=result.execution_id,
                details={
                    "workflow_id": payload.workflow_id,
                    "step_id": result.step_id,
                    "execution_status": result.execution_status,
                    "outcome": result.outcome,
                },
            )
            return result
        except (
            PluginResolutionError,
            SkillResolutionError,
            CapabilityResolutionError,
            ImageResolutionError,
        ) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SandboxExecutionError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except ImageBuildError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @application.get("/v1/agent-steps/{execution_id}", response_model=StepResult)
    def get_agent_step(execution_id: str, request: Request) -> StepResult:
        if not IDENTIFIER_PATTERN.fullmatch(execution_id):
            raise HTTPException(status_code=400, detail="invalid execution_id")
        result = request.app.state.service.job_store.get(execution_id)
        if result is None:
            raise HTTPException(status_code=404, detail="step result not found")
        return result

    @application.get("/v1/artifacts", response_model=list[ArtifactRecord])
    def artifacts(request: Request, execution_id: str | None = None) -> list[ArtifactRecord]:
        if execution_id is not None and not IDENTIFIER_PATTERN.fullmatch(execution_id):
            raise HTTPException(status_code=400, detail="invalid execution_id")
        return request.app.state.service.job_store.artifacts.list(execution_id)

    @application.post("/v1/artifacts/cleanup", response_model=ArtifactCleanupResult)
    def cleanup_artifacts(request: Request) -> ArtifactCleanupResult:
        result = request.app.state.service.job_store.cleanup_expired()
        _audit_request(
            request,
            action="artifacts.cleanup.completed",
            resource_type="artifact-registry",
            details=result.model_dump(mode="json"),
        )
        return result

    @application.get("/v1/agent-steps/{execution_id}/artifacts/{artifact_path:path}")
    def get_artifact(execution_id: str, artifact_path: str, request: Request) -> FileResponse:
        if not IDENTIFIER_PATTERN.fullmatch(execution_id):
            raise HTTPException(status_code=400, detail="invalid execution_id")
        artifact = request.app.state.service.job_store.artifact(execution_id, artifact_path)
        if artifact is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        return FileResponse(artifact)

    return application


app = create_app()
