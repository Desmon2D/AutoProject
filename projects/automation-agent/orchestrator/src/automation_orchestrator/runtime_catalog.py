from __future__ import annotations

import os
from collections.abc import Iterable

from .models import AgentModelDefinition, CredentialReference, ScenarioManifest


def _csv_environment(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def agent_models(scenarios: Iterable[ScenarioManifest]) -> list[AgentModelDefinition]:
    default_provider = os.environ.get("DEFAULT_AGENT_PROVIDER", "openrouter").strip()
    default_model = os.environ.get("DEFAULT_AGENT_MODEL", "z-ai/glm-4.7-flash").strip()
    configured = {
        "openai": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
        "openrouter": bool(os.environ.get("OPENROUTER_API_KEY", "").strip()),
    }
    candidates: set[tuple[str, str]] = set()
    if default_provider in configured and default_model:
        candidates.add((default_provider, default_model))
    openai_models = _csv_environment("OPENAI_MODELS") or ["gpt-5.4"]
    candidates.update(("openai", model) for model in openai_models)
    candidates.update(("openrouter", model) for model in _csv_environment("OPENROUTER_MODELS"))
    for scenario in scenarios:
        for step in scenario.steps.values():
            if step.type == "agent":
                candidates.add((step.provider, step.model))
    return [
        AgentModelDefinition(
            id=model,
            provider=provider,
            title=model,
            configured=configured[provider],
            default=provider == default_provider and model == default_model,
        )
        for provider, model in sorted(candidates, key=lambda item: (item[0], item[1]))
    ]


CREDENTIAL_PROVIDERS = {
    "openai-default": "openai",
    "openrouter-default": "openrouter",
    "gitea-default": "gitea",
    "plane-default": "plane",
}


def credential_references() -> list[CredentialReference]:
    environment = {
        "openai-default": "OPENAI_API_KEY",
        "openrouter-default": "OPENROUTER_API_KEY",
        "gitea-default": "GITEA_TOKEN",
        "plane-default": "PLANE_API_TOKEN",
    }
    return [
        CredentialReference(
            id=credential_id,
            provider=provider,
            title=f"{provider} · default",
            configured=bool(os.environ.get(environment[credential_id], "").strip()),
        )
        for credential_id, provider in CREDENTIAL_PROVIDERS.items()
    ]
