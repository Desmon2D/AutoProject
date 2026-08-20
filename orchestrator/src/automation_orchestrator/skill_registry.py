from __future__ import annotations

from pathlib import Path

from .models import SkillManifest, SkillResolution


class SkillResolutionError(ValueError):
    pass


class SkillRegistry:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self._manifests = self._load()

    def _load(self) -> dict[str, SkillManifest]:
        manifests: dict[str, SkillManifest] = {}
        if not self.root.exists():
            return manifests
        for path in sorted(self.root.glob("*/skill.json")):
            manifest = SkillManifest.model_validate_json(path.read_text(encoding="utf-8"))
            if manifest.name in manifests:
                raise RuntimeError(f"duplicate skill: {manifest.name}")
            skill_path = (path.parent / manifest.skill_file).resolve()
            if self.root not in skill_path.parents or not skill_path.is_file():
                raise RuntimeError(f"invalid skill_file for skill {manifest.name}")
            manifests[manifest.name] = manifest
        return manifests

    def list(self) -> list[SkillManifest]:
        return list(self._manifests.values())

    def resolve(self, requested: list[str]) -> SkillResolution:
        manifests: list[SkillManifest] = []
        for name in requested:
            manifest = self._manifests.get(name)
            if manifest is None:
                raise SkillResolutionError(f"unknown skill: {name}")
            if not manifest.enabled:
                reason = manifest.unavailable_reason or "skill is disabled"
                raise SkillResolutionError(f"skill {name} is unavailable: {reason}")
            manifests.append(manifest)

        commands = sorted({command for item in manifests for command in item.requires_commands})
        capabilities = sorted(
            {capability for item in manifests for capability in item.requires_capabilities}
        )
        return SkillResolution(
            skills=manifests,
            required_capabilities=capabilities,
            required_commands=commands,
        )

    def files(self, resolution: SkillResolution) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        for manifest in resolution.skills:
            path = (self.root / manifest.name / manifest.skill_file).resolve()
            files[f".agents/skills/{manifest.name}/SKILL.md"] = path.read_bytes()
        return files
