from __future__ import annotations

from pathlib import Path

import pytest

from automation_orchestrator.capability_registry import CapabilityRegistry
from automation_orchestrator.image_registry import ImageRegistry
from automation_orchestrator.image_resolver import AgentImageResolver
from automation_orchestrator.plugin_registry import PluginRegistry
from automation_orchestrator.skill_registry import SkillRegistry


class StubImageBuilder:
    def __init__(self) -> None:
        self.ensured: list[str] = []

    def ensure(self, spec, plugins) -> str:
        self.ensured.append(spec.image)
        return spec.image


@pytest.fixture
def plugin_root() -> Path:
    return Path(__file__).parents[1] / "plugins"


@pytest.fixture
def skill_root() -> Path:
    return Path(__file__).parents[1] / "skills"


@pytest.fixture
def capability_root() -> Path:
    return Path(__file__).parents[1] / "capabilities"


@pytest.fixture
def image_root() -> Path:
    return Path(__file__).parents[1] / "image-catalog"


@pytest.fixture
def image_resolver(plugin_root, skill_root, capability_root, image_root):
    plugins = PluginRegistry(plugin_root)
    capabilities = CapabilityRegistry(capability_root)
    return AgentImageResolver(
        plugins,
        SkillRegistry(skill_root),
        ImageRegistry(image_root, capabilities),
        StubImageBuilder(),
    )
