from __future__ import annotations

from pathlib import Path

from .models import CapabilityManifest


class CapabilityResolutionError(ValueError):
    pass


class CapabilityRegistry:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self._manifests = self._load()

    def _load(self) -> dict[str, CapabilityManifest]:
        manifests: dict[str, CapabilityManifest] = {}
        if not self.root.exists():
            return manifests
        for path in sorted(self.root.glob("*/capability.json")):
            manifest = CapabilityManifest.model_validate_json(path.read_text(encoding="utf-8"))
            if manifest.name in manifests:
                raise RuntimeError(f"duplicate capability: {manifest.name}")
            manifests[manifest.name] = manifest
        return manifests

    def list(self) -> list[CapabilityManifest]:
        return list(self._manifests.values())

    def require(self, requested: list[str]) -> list[CapabilityManifest]:
        resolved: list[CapabilityManifest] = []
        for name in requested:
            manifest = self._manifests.get(name)
            if manifest is None:
                raise CapabilityResolutionError(f"unknown capability: {name}")
            if not manifest.enabled:
                raise CapabilityResolutionError(f"capability is disabled: {name}")
            resolved.append(manifest)
        return resolved
