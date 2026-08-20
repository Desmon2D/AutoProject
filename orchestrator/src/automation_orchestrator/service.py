from __future__ import annotations

import os
from typing import Any

from .context_builder import ContextBuilder
from .image_resolver import AgentImageResolver
from .job_store import JobStore
from .models import (
    AgentImageResolution,
    AgentRunRequest,
    ArtifactRef,
    PreparedAgentStep,
    SandboxResult,
    StepError,
    StepResult,
)
from .sandbox_manager import SandboxManager

REQUIRED_AGENT_PLUGINS = ("step-result",)


class AgentService:
    def __init__(
        self,
        context_builder: ContextBuilder,
        image_resolver: AgentImageResolver,
        sandbox_manager: SandboxManager,
    ):
        self.context_builder = context_builder
        self.image_resolver = image_resolver
        self.plugin_registry = image_resolver.plugin_registry
        self.skill_registry = image_resolver.skill_registry
        self.image_registry = image_resolver.image_registry
        self.capability_registry = image_resolver.image_registry.capability_registry
        self.sandbox_manager = sandbox_manager
        self.job_store = JobStore(
            sandbox_manager.jobs_root,
            artifact_ttl_seconds=int(os.environ.get("ARTIFACT_TTL_SECONDS", "2592000")),
        )

    def prepare(self, request: AgentRunRequest) -> PreparedAgentStep:
        built_context = self.context_builder.build(request.step, request.context)
        resolution = self._resolve(request)
        task: dict[str, Any] = {
            "job_id": request.execution_id,
            "prompt": built_context.prompt,
            "provider": request.step.provider,
            "model": request.step.model,
            "timeout_seconds": request.step.timeout_seconds,
            "plugins": [plugin.name for plugin in resolution.plugins],
            "context_digest": built_context.digest,
        }
        return PreparedAgentStep(
            execution_id=request.execution_id,
            image=resolution.image,
            image_spec=resolution.image_spec,
            plugins=self._plugin_names(request),
            required_commands=resolution.required_commands,
            context=built_context,
            task=task,
        )

    def run(self, request: AgentRunRequest) -> StepResult:
        prepared = self.prepare(request)
        cached = self.job_store.begin(request)
        if cached is not None:
            return cached
        resolution = self._resolve(request, ensure_image=True)
        sandbox_result = self.sandbox_manager.run(
            execution_id=request.execution_id,
            task=prepared.task,
            resolution=resolution,
            skill_files=self.skill_registry.files(
                self.skill_registry.resolve([skill.name for skill in resolution.skills])
            ),
        )
        result = self._normalize(request, sandbox_result)
        self.job_store.save(result)
        return result

    def _resolve(
        self, request: AgentRunRequest, *, ensure_image: bool = False
    ) -> AgentImageResolution:
        return self.image_resolver.resolve(
            plugin_names=self._plugin_names(request),
            ensure_image=ensure_image,
        )

    @staticmethod
    def _plugin_names(request: AgentRunRequest) -> list[str]:
        return list(dict.fromkeys([*REQUIRED_AGENT_PLUGINS, *request.step.plugins]))

    @staticmethod
    def _normalize(request: AgentRunRequest, result: SandboxResult) -> StepResult:
        artifacts: list[ArtifactRef] = []
        for artifact in result.artifacts:
            path = artifact.get("path")
            uri = artifact.get("uri") or artifact.get("ref")
            if path:
                uri = f"artifact://{request.execution_id}/{str(path).lstrip('/')}"
            if not uri:
                continue
            artifacts.append(
                ArtifactRef(
                    type=str(artifact.get("type", "file")),
                    uri=str(uri),
                    summary=(
                        str(artifact["summary"]) if artifact.get("summary") is not None else None
                    ),
                )
            )

        data: dict[str, Any] = dict(result.data)
        data.update(
            {
                "summary": result.summary,
                "provider": result.provider,
                "model": result.model,
                "duration_ms": result.duration_ms,
            }
        )
        if result.status == "success":
            return StepResult(
                step_id=request.step.id,
                execution_id=request.execution_id,
                iteration=request.iteration,
                attempt=request.attempt,
                execution_status="COMPLETED",
                outcome="SUCCESS",
                data=data,
                artifacts=artifacts,
            )
        if result.status == "failure":
            return StepResult(
                step_id=request.step.id,
                execution_id=request.execution_id,
                iteration=request.iteration,
                attempt=request.attempt,
                execution_status="COMPLETED",
                outcome="FAILURE",
                data=data,
                artifacts=artifacts,
            )

        raw_error = result.error or {}
        error = StepError(
            code=str(raw_error.get("code", "SANDBOX_ERROR")),
            message=str(raw_error.get("message", result.summary)),
            retryable=bool(raw_error.get("retryable", False)),
        )
        return StepResult(
            step_id=request.step.id,
            execution_id=request.execution_id,
            iteration=request.iteration,
            attempt=request.attempt,
            execution_status="ERROR",
            outcome=None,
            data=data,
            artifacts=artifacts,
            error=error,
        )
