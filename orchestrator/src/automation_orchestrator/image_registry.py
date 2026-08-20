from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .capability_registry import CapabilityRegistry
from .models import ImageProfileManifest, ImageSpec, PluginManifest


class ImageResolutionError(ValueError):
    pass


class ImageRegistry:
    def __init__(
        self,
        root: Path,
        capability_registry: CapabilityRegistry,
        *,
        dynamic_image_prefix: str = "automation-dsh-sandbox-custom",
    ):
        self.root = root.resolve()
        self.capability_registry = capability_registry
        self.dynamic_image_prefix = dynamic_image_prefix
        self._profiles = self._load()

    def _load(self) -> dict[str, ImageProfileManifest]:
        profiles: dict[str, ImageProfileManifest] = {}
        if not self.root.exists():
            return profiles
        known_capabilities = {item.name for item in self.capability_registry.list()}
        for path in sorted(self.root.glob("*.json")):
            profile = ImageProfileManifest.model_validate_json(path.read_text(encoding="utf-8"))
            if profile.name in profiles:
                raise RuntimeError(f"duplicate image profile: {profile.name}")
            unknown = set(profile.capabilities) - known_capabilities
            if unknown:
                raise RuntimeError(
                    f"image profile {profile.name} has unknown capabilities: {sorted(unknown)}"
                )
            profiles[profile.name] = profile
        return profiles

    def list(self) -> list[ImageProfileManifest]:
        return list(self._profiles.values())

    def resolve(
        self,
        *,
        capabilities: list[str],
        plugins: list[PluginManifest],
    ) -> ImageSpec:
        self.capability_registry.require(capabilities)
        required_capabilities = set(capabilities)
        requested_plugins = {plugin.name for plugin in plugins}
        candidates = [
            profile
            for profile in self._profiles.values()
            if profile.enabled and required_capabilities.issubset(profile.capabilities)
        ]
        if not candidates:
            raise ImageResolutionError(
                f"no sandbox profile provides capabilities: {sorted(required_capabilities)}"
            )
        candidates.sort(
            key=lambda item: (
                len(set(item.capabilities) - required_capabilities),
                len(item.capabilities),
                len(requested_plugins.symmetric_difference(item.plugins)),
                item.name,
            )
        )
        profile = candidates[0]
        missing_plugins = sorted(requested_plugins - set(profile.plugins))
        for plugin in plugins:
            if plugin.name in missing_plugins and plugin.built_into_image:
                raise ImageResolutionError(
                    f"plugin {plugin.name} is marked built-in but is missing from profile {profile.name}"
                )
            if plugin.name in missing_plugins and plugin.source_dir is None:
                raise ImageResolutionError(f"plugin {plugin.name} has no source for image assembly")

        canonical = {
            "schema_version": 1,
            "profile": profile.name,
            "base_image": profile.image,
            "harness_version": profile.harness_version,
            "capabilities": sorted(set(profile.capabilities)),
            "plugins": [
                {"name": plugin.name, "version": plugin.version}
                for plugin in sorted(plugins, key=lambda item: item.name)
            ],
        }
        digest = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        requires_build = bool(missing_plugins)
        image = f"{self.dynamic_image_prefix}:{digest[:20]}" if requires_build else profile.image
        return ImageSpec(
            profile=profile.name,
            base_image=profile.image,
            image=image,
            harness_version=profile.harness_version,
            capabilities=sorted(set(profile.capabilities)),
            plugins=sorted(requested_plugins),
            digest=digest,
            requires_build=requires_build,
        )
