from __future__ import annotations

import os
from pathlib import Path

from .audit_store import AuditStore
from .capability_registry import CapabilityRegistry
from .context_builder import ContextBuilder
from .image_builder import DockerImageBuilder
from .image_registry import ImageRegistry
from .image_resolver import AgentImageResolver
from .plugin_registry import PluginRegistry
from .sandbox_manager import SandboxManager
from .scenario_registry import ScenarioRegistry
from .service import AgentService
from .skill_registry import SkillRegistry
from .swirl_client import SwirlClient
from .workflow_engine import WorkflowEngine
from .workflow_queue import WorkflowQueue
from .workflow_store import WorkflowStore


def build_service() -> AgentService:
    plugin_root = Path(os.environ.get("PLUGIN_ROOT", "/app/plugins"))
    skill_root = Path(os.environ.get("SKILL_ROOT", "/app/skills"))
    capability_root = Path(os.environ.get("CAPABILITY_ROOT", "/app/capabilities"))
    image_root = Path(os.environ.get("IMAGE_ROOT", "/app/images"))
    scenario_root = Path(os.environ.get("SCENARIO_ROOT", "/app/scenarios"))
    jobs_root = Path(os.environ.get("JOBS_ROOT", "/data/jobs"))
    queue_path = Path(
        os.environ.get("WORKFLOW_QUEUE_PATH", str(jobs_root / "workflow-queue.sqlite3"))
    )
    plugin_registry = PluginRegistry(plugin_root)
    skill_registry = SkillRegistry(skill_root)
    capability_registry = CapabilityRegistry(capability_root)
    image_registry = ImageRegistry(
        image_root,
        capability_registry,
        dynamic_image_prefix=os.environ.get(
            "DYNAMIC_IMAGE_PREFIX", "automation-dsh-sandbox-custom"
        ),
    )
    image_resolver = AgentImageResolver(
        plugin_registry,
        skill_registry,
        image_registry,
        DockerImageBuilder(plugin_registry),
    )
    service = AgentService(
        ContextBuilder(max_characters=int(os.environ.get("CONTEXT_MAX_CHARACTERS", "24000"))),
        image_resolver,
        SandboxManager(
            jobs_root,
            network=os.environ.get("SANDBOX_MODEL_NETWORK")
            or os.environ.get("SANDBOX_NETWORK")
            or None,
            plugin_networks={
                "gitea": os.environ.get("GITEA_SANDBOX_NETWORK", "").strip(),
                "swirl": os.environ.get("SWIRL_SANDBOX_NETWORK", "").strip(),
            },
        ),
    )
    service.scenario_registry = ScenarioRegistry(
        scenario_root,
        agent_step_validator=lambda step: image_resolver.resolve(
            plugin_names=["step-result", *step.plugins],
        ),
    )
    service.audit_store = AuditStore(jobs_root / "audit.sqlite3")
    service.workflow_engine = WorkflowEngine(
        service.scenario_registry,
        WorkflowStore(jobs_root / "workflows"),
        service,
        swirl_client=SwirlClient.from_environment(),
        audit_store=service.audit_store,
    )
    service.workflow_queue = WorkflowQueue(queue_path)
    return service
