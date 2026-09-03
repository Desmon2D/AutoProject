import pytest

from automation_orchestrator.capability_registry import CapabilityRegistry
from automation_orchestrator.image_registry import ImageRegistry
from automation_orchestrator.plugin_registry import PluginRegistry, PluginResolutionError
from automation_orchestrator.skill_registry import SkillRegistry, SkillResolutionError


def test_resolves_native_plugin(plugin_root):
    registry = PluginRegistry(plugin_root)
    resolution = registry.resolve(["step-result"])

    assert resolution.plugins[0].npm_package == "@automation/dsh-plugin-step-result"
    assert resolution.plugins[0].built_into_image is True


def test_resolves_and_materializes_skills(skill_root):
    registry = SkillRegistry(skill_root)
    resolution = registry.resolve(["git", "python"])

    assert resolution.required_commands == ["git", "python3"]
    assert set(registry.files(resolution)) == {
        ".agents/skills/git/SKILL.md",
        ".agents/skills/python/SKILL.md",
    }


def test_rejects_unknown_plugin_and_resolves_swirl(plugin_root):
    registry = PluginRegistry(plugin_root)
    with pytest.raises(PluginResolutionError, match="unknown plugin"):
        registry.resolve(["missing"])
    swirl = registry.resolve(["swirl"]).plugins[0]
    assert swirl.npm_package == "@automation/dsh-plugin-swirl"
    assert swirl.required_environment == [
        "SWIRL_BASE_URL",
        "SWIRL_USERNAME",
        "SWIRL_PASSWORD",
        "SWIRL_CONTENT_ALLOWED_ORIGINS",
        "SWIRL_CONTENT_ROUTES_JSON",
    ]


def test_rejects_unknown_skill(skill_root):
    registry = SkillRegistry(skill_root)
    with pytest.raises(SkillResolutionError, match="unknown skill"):
        registry.resolve(["missing"])


def test_selects_smallest_compatible_image(plugin_root, capability_root, image_root):
    plugins = PluginRegistry(plugin_root).resolve(["step-result"]).plugins
    registry = ImageRegistry(image_root, CapabilityRegistry(capability_root))

    core = registry.resolve(capabilities=[], plugins=plugins)
    code = registry.resolve(capabilities=["git"], plugins=plugins)

    assert core.profile == "core"
    assert core.requires_build is False
    assert code.profile == "code"
    assert code.image == "automation-dsh-sandbox-code:0.1.0-rc.7"


def test_gitea_plugin_requires_custom_code_image(plugin_root, capability_root, image_root):
    plugins = PluginRegistry(plugin_root).resolve(["step-result", "gitea"]).plugins
    registry = ImageRegistry(image_root, CapabilityRegistry(capability_root))

    spec = registry.resolve(capabilities=["git"], plugins=plugins)

    assert spec.profile == "code"
    assert spec.requires_build is True
    assert spec.image.startswith("automation-dsh-sandbox-custom:")
    assert spec.plugins == ["gitea", "step-result"]


def test_swirl_plugin_requires_custom_core_image(plugin_root, capability_root, image_root):
    plugins = PluginRegistry(plugin_root).resolve(["step-result", "swirl"]).plugins
    registry = ImageRegistry(image_root, CapabilityRegistry(capability_root))

    spec = registry.resolve(capabilities=[], plugins=plugins)

    assert spec.profile == "core"
    assert spec.requires_build is True
    assert spec.plugins == ["step-result", "swirl"]


def test_delivery_profile_covers_gitea_and_swirl(plugin_root, capability_root, image_root):
    plugins = PluginRegistry(plugin_root).resolve(["step-result", "gitea", "swirl"]).plugins
    registry = ImageRegistry(image_root, CapabilityRegistry(capability_root))

    spec = registry.resolve(capabilities=["git"], plugins=plugins)

    assert spec.profile == "delivery"
    assert spec.requires_build is False
    assert spec.image == "automation-dsh-sandbox-delivery:0.1.0-rc.7"
