from __future__ import annotations

from .image_builder import DockerImageBuilder
from .image_registry import ImageRegistry
from .models import AgentImageResolution
from .plugin_registry import PluginRegistry
from .skill_registry import SkillRegistry

TOOL_PLUGIN_SKILLS = {"git": "git", "python": "python"}


class AgentImageResolver:
    def __init__(
        self,
        plugin_registry: PluginRegistry,
        skill_registry: SkillRegistry,
        image_registry: ImageRegistry,
        image_builder: DockerImageBuilder,
    ):
        self.plugin_registry = plugin_registry
        self.skill_registry = skill_registry
        self.image_registry = image_registry
        self.image_builder = image_builder

    def resolve(
        self,
        *,
        plugin_names: list[str],
        ensure_image: bool = False,
    ) -> AgentImageResolution:
        runtime_plugin_names = [name for name in plugin_names if name not in TOOL_PLUGIN_SKILLS]
        skill_names = [skill for name, skill in TOOL_PLUGIN_SKILLS.items() if name in plugin_names]
        plugins = self.plugin_registry.resolve(runtime_plugin_names)
        skills = self.skill_registry.resolve(skill_names)
        capabilities = sorted(
            {
                *skills.required_capabilities,
                *(item for plugin in plugins.plugins for item in plugin.requires_capabilities),
            }
        )
        capability_manifests = self.image_registry.capability_registry.require(capabilities)
        required_commands = sorted(
            {
                *skills.required_commands,
                *(
                    command
                    for capability in capability_manifests
                    for command in capability.commands
                ),
            }
        )
        image_spec = self.image_registry.resolve(
            capabilities=capabilities,
            plugins=plugins.plugins,
        )
        if ensure_image:
            self.image_builder.ensure(image_spec, plugins.plugins)
        return AgentImageResolution(
            image=image_spec.image,
            image_spec=image_spec,
            plugins=plugins.plugins,
            skills=skills.skills,
            required_commands=required_commands,
        )
