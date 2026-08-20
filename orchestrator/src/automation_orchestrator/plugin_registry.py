from __future__ import annotations

from pathlib import Path

from .models import PluginManifest, PluginResolution


class PluginResolutionError(ValueError):
    pass


class PluginRegistry:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self._manifests = self._load()

    def _load(self) -> dict[str, PluginManifest]:
        manifests: dict[str, PluginManifest] = {}
        if not self.root.exists():
            return manifests
        for path in sorted(self.root.glob("*/plugin.json")):
            manifest = PluginManifest.model_validate_json(path.read_text(encoding="utf-8"))
            if manifest.name in manifests:
                raise RuntimeError(f"duplicate plugin: {manifest.name}")
            manifests[manifest.name] = manifest
        return manifests

    def list(self) -> list[PluginManifest]:
        return list(self._manifests.values())

    def resolve(self, requested: list[str]) -> PluginResolution:
        manifests: list[PluginManifest] = []
        for name in requested:
            manifest = self._manifests.get(name)
            if manifest is None:
                raise PluginResolutionError(f"unknown plugin: {name}")
            if not manifest.enabled:
                reason = manifest.unavailable_reason or "plugin is disabled"
                raise PluginResolutionError(f"plugin {name} is unavailable: {reason}")
            manifests.append(manifest)

        return PluginResolution(plugins=manifests)

    def source(self, manifest: PluginManifest) -> Path:
        if manifest.source_dir is None:
            raise PluginResolutionError(f"plugin {manifest.name} has no build source")
        source = (self.root / manifest.name / manifest.source_dir).resolve()
        if self.root not in source.parents or not source.is_dir():
            raise PluginResolutionError(f"plugin {manifest.name} has an invalid build source")
        return source
