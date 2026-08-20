import json
import os
from pathlib import Path

import docker
import pytest

from automation_orchestrator.capability_registry import CapabilityRegistry
from automation_orchestrator.image_builder import DockerImageBuilder
from automation_orchestrator.image_registry import ImageRegistry
from automation_orchestrator.plugin_registry import PluginRegistry


@pytest.mark.docker
@pytest.mark.skipif(
    os.environ.get("HARNES_DOCKER_E2E") != "1",
    reason="set HARNES_DOCKER_E2E=1 to build the Gitea sandbox image",
)
def test_builds_and_loads_production_gitea_plugin():
    root = Path(__file__).parents[1]
    plugins = PluginRegistry(root / "plugins")
    selected = plugins.resolve(["step-result", "gitea"]).plugins
    images = ImageRegistry(root / "images", CapabilityRegistry(root / "capabilities"))
    spec = images.resolve(capabilities=["git"], plugins=selected)
    client = docker.from_env()

    DockerImageBuilder(plugins, client_factory=lambda: client).ensure(spec, selected)
    payload = client.containers.run(
        spec.image,
        ["cat", "/opt/sandbox/image-manifest.json"],
        network_disabled=True,
        remove=True,
    )
    manifest = json.loads(payload)
    assert set(manifest["plugins"]) == {"gitea", "step-result"}

    test_path = (
        "/usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/"
        "@automation/dsh-plugin-gitea/test/index.test.js"
    )
    output = client.containers.run(
        spec.image,
        ["node", "--test", test_path],
        environment={
            "TEST_GITEA_URL": "https://gitea.example.test",
            "TEST_GITEA_TOKEN": "test-token",
            "GITEA_ALLOWED_REPOSITORIES": "team/service",
        },
        network_disabled=True,
        remove=True,
    )
    assert b"fail 0" in output
