from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .models import ScenarioManifest, TriggerEvent, WorkflowInstance


class WorkflowStore:
    def __init__(self, root: Path):
        self.root = root

    @staticmethod
    def workflow_id(scenario: ScenarioManifest, event: TriggerEvent) -> str:
        source = f"{scenario.id}\0{event.source}\0{event.event}\0{event.event_id}"
        return f"wf-{hashlib.sha256(source.encode('utf-8')).hexdigest()[:24]}"

    @staticmethod
    def scenario_digest(scenario: ScenarioManifest) -> str:
        canonical = json.dumps(
            scenario.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def save_scenario_snapshot(self, workflow_id: str, scenario: ScenarioManifest) -> str:
        directory = self.root / workflow_id
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "scenario.json"
        digest = self.scenario_digest(scenario)
        if target.is_file():
            stored = ScenarioManifest.model_validate_json(target.read_text(encoding="utf-8"))
            if self.scenario_digest(stored) != digest:
                raise RuntimeError("workflow scenario snapshot is immutable")
            return digest
        temporary = directory / "scenario.json.tmp"
        temporary.write_text(scenario.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(target)
        return digest

    def get_scenario_snapshot(self, workflow_id: str) -> ScenarioManifest | None:
        path = self.root / workflow_id / "scenario.json"
        if not path.is_file():
            return None
        return ScenarioManifest.model_validate_json(path.read_text(encoding="utf-8"))

    def get(self, workflow_id: str) -> WorkflowInstance | None:
        path = self.root / workflow_id / "workflow.json"
        if not path.is_file():
            return None
        return WorkflowInstance.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self) -> list[WorkflowInstance]:
        if not self.root.is_dir():
            return []
        workflows: list[WorkflowInstance] = []
        for path in self.root.glob("*/workflow.json"):
            workflows.append(WorkflowInstance.model_validate_json(path.read_text(encoding="utf-8")))
        return sorted(workflows, key=lambda item: item.updated_at, reverse=True)

    def save(self, workflow: WorkflowInstance) -> None:
        directory = self.root / workflow.id
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "workflow.json"
        temporary = directory / "workflow.json.tmp"
        temporary.write_text(workflow.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(target)

    def mark_cancel_requested(self, workflow_id: str) -> None:
        directory = self.root / workflow_id
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "cancel.requested"
        temporary = directory / "cancel.requested.tmp"
        temporary.write_text("requested\n", encoding="ascii")
        temporary.replace(target)

    def is_cancel_requested(self, workflow_id: str) -> bool:
        return (self.root / workflow_id / "cancel.requested").is_file()

    def clear_cancel_requested(self, workflow_id: str) -> None:
        marker = self.root / workflow_id / "cancel.requested"
        if marker.is_file():
            marker.unlink()
