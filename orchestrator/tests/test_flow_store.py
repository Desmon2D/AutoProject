from pathlib import Path

import pytest

from automation_orchestrator.flow_builder import scenario_to_flow
from automation_orchestrator.flow_store import FlowRevisionConflict, FlowStore
from automation_orchestrator.flow_validation import validate_flow
from automation_orchestrator.scenario_registry import ScenarioRegistry


def _draft_source():
    registry = ScenarioRegistry(Path(__file__).parents[1] / "scenarios")
    source = scenario_to_flow(registry.get("bug-finding"))
    return source.model_copy(
        update={
            "id": "bug-finding-copy",
            "title": "Bug finding copy",
            "builtin": False,
            "read_only": False,
            "status": "draft",
            "nodes": [node.model_copy(update={"read_only": False}) for node in source.nodes],
        }
    )


def test_flow_store_revision_locking_and_immutable_versions(tmp_path: Path):
    store = FlowStore(tmp_path / "flows.sqlite3")
    created = store.create(_draft_source())

    assert created.revision == 1
    assert created.version == "draft"
    assert created.read_only is False

    changed = created.model_copy(update={"title": "Changed draft"})
    saved = store.save(changed, expected_revision=1)
    assert saved.revision == 2
    assert saved.title == "Changed draft"
    with pytest.raises(FlowRevisionConflict):
        store.save(changed, expected_revision=1)

    published = store.publish(saved.id, expected_revision=2)
    assert published.version == 1
    assert published.definition.status == "published"
    assert published.definition.read_only is True
    assert len(published.sha256) == 64
    assert store.versions(saved.id) == [published]

    store.delete(saved.id, expected_revision=2)
    assert store.get(saved.id) is None
    assert store.versions(saved.id) == [published]


def test_flow_store_matches_only_latest_enabled_published_trigger(tmp_path: Path):
    store = FlowStore(tmp_path / "flows.sqlite3")
    source = _draft_source()
    trigger = next(node for node in source.nodes if node.type == "trigger")
    source = source.model_copy(
        update={
            "nodes": [
                node.model_copy(
                    update={"config": {"source": "webhook", "event": "webhook.received"}}
                )
                if node.id == trigger.id
                else node
                for node in source.nodes
            ]
        }
    )
    draft = store.create(source)
    first = store.publish(draft.id, expected_revision=draft.revision)
    saved = store.save(draft.model_copy(update={"title": "Version two"}), expected_revision=1)
    second = store.publish(saved.id, expected_revision=saved.revision)

    matches = store.matching_versions("webhook", "webhook.received")

    assert [version.version for version in matches] == [second.version]
    assert matches[0].sha256 != first.sha256
    assert store.matching_versions("webhook", "unsupported") == []

def test_flow_validator_rejects_broken_transition():
    flow = _draft_source()
    broken = flow.model_copy(update={"edges": flow.edges[1:]})

    result = validate_flow(broken)

    assert result.valid is False
    assert {issue.code for issue in result.errors} >= {
        "trigger-start-mismatch",
    }
    assert result.sha256 is None


def test_flow_validator_accepts_builtin_graph():
    result = validate_flow(_draft_source())

    assert result.valid is True
    assert result.errors == []
    assert result.sha256 is not None


def test_flow_validator_rejects_invalid_node_config_and_duplicate_port():
    flow = _draft_source()
    agent = next(node for node in flow.nodes if node.type == "agent")
    broken_agent = agent.model_copy(update={"config": {**agent.config, "prompt": ""}})
    original_edge = next(edge for edge in flow.edges if edge.source == agent.id)
    duplicate_edge = original_edge.model_copy(
        update={"id": f"{original_edge.id}:duplicate"}
    )
    broken = flow.model_copy(
        update={
            "nodes": [broken_agent if node.id == agent.id else node for node in flow.nodes],
            "edges": [*flow.edges, duplicate_edge],
        }
    )

    result = validate_flow(broken)

    assert result.valid is False
    assert {issue.code for issue in result.errors} >= {
        "agent-prompt-missing",
        "duplicate-output-port",
    }
