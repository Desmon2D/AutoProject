from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .models import AgentScenarioStep, ScenarioManifest, TriggerEvent


class ScenarioResolutionError(ValueError):
    pass


class ScenarioRegistry:
    def __init__(
        self,
        root: Path,
        *,
        agent_step_validator: Callable[[AgentScenarioStep], None] | None = None,
    ):
        self.root = root.resolve()
        self.agent_step_validator = agent_step_validator
        self._scenarios = self._load()

    def _load(self) -> dict[str, ScenarioManifest]:
        scenarios: dict[str, ScenarioManifest] = {}
        if not self.root.exists():
            return scenarios
        for path in sorted(self.root.glob("*.json")):
            scenario = ScenarioManifest.model_validate_json(path.read_text(encoding="utf-8"))
            if self.agent_step_validator is not None:
                for step in scenario.steps.values():
                    if isinstance(step, AgentScenarioStep):
                        self.agent_step_validator(step)
            if scenario.id in scenarios:
                raise RuntimeError(f"duplicate scenario: {scenario.id}")
            scenarios[scenario.id] = scenario
        return scenarios

    def list(self) -> list[ScenarioManifest]:
        return list(self._scenarios.values())

    def get(self, scenario_id: str) -> ScenarioManifest:
        scenario = self._scenarios.get(scenario_id)
        if scenario is None:
            raise ScenarioResolutionError(f"unknown scenario: {scenario_id}")
        return scenario

    def match(self, event: TriggerEvent) -> ScenarioManifest:
        matches = [
            scenario
            for scenario in self._scenarios.values()
            if scenario.enabled
            and scenario.trigger.source == event.source
            and scenario.trigger.event == event.event
        ]
        if not matches:
            raise ScenarioResolutionError(
                f"no scenario matches trigger: {event.source}.{event.event}"
            )
        if len(matches) > 1:
            raise ScenarioResolutionError(
                f"multiple scenarios match trigger: {event.source}.{event.event}"
            )
        return matches[0]
