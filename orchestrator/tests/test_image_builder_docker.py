import json
import os
import uuid
from pathlib import Path

import docker
import pytest

from automation_orchestrator.image_builder import DockerImageBuilder
from automation_orchestrator.models import ImageSpec, PluginManifest
from automation_orchestrator.plugin_registry import PluginRegistry


@pytest.mark.docker
@pytest.mark.skipif(
    os.environ.get("HARNES_DOCKER_E2E") != "1",
    reason="set HARNES_DOCKER_E2E=1 to build a custom sandbox image",
)
def test_builds_and_reuses_custom_plugin_image(tmp_path: Path):
    plugin_root = tmp_path / "plugins"
    source = plugin_root / "example" / "source"
    source.mkdir(parents=True)
    (source / "index.js").write_text("export function apply() {}\n", encoding="utf-8")
    example = PluginManifest(
        name="example",
        version="1.0.0",
        description="Docker smoke plugin",
        npm_package="@automation/dsh-plugin-example",
        entrypoint="index.js",
        source_dir="source",
    )
    step_result = PluginManifest(
        name="step-result",
        version="0.1.0",
        description="Mandatory result plugin",
        npm_package="@automation/dsh-plugin-step-result",
        entrypoint="index.js",
        inject=["tools", "systemPrompt"],
        built_into_image=True,
        mandatory=True,
    )
    suffix = uuid.uuid4().hex[:12]
    spec = ImageSpec(
        profile="core",
        base_image="automation-dsh-sandbox-core:0.1.0-rc.7",
        image=f"automation-dsh-sandbox-custom:smoke-{suffix}",
        harness_version="0.1.0-rc.7",
        capabilities=["node", "python"],
        plugins=["example", "step-result"],
        digest=suffix,
        requires_build=True,
    )
    client = docker.from_env()
    builder = DockerImageBuilder(PluginRegistry(plugin_root), client_factory=lambda: client)

    try:
        assert builder.ensure(spec, [step_result, example]) == spec.image
        assert builder.ensure(spec, [step_result, example]) == spec.image
        payload = client.containers.run(
            spec.image,
            ["cat", "/opt/sandbox/image-manifest.json"],
            network_disabled=True,
            remove=True,
        )
        manifest = json.loads(payload)
        assert set(manifest["plugins"]) == {"example", "step-result"}
        assert manifest["plugins"]["step-result"]["mandatory"] is True
    finally:
        client.images.remove(spec.image, force=True)
