import json
import tarfile
from pathlib import Path

from automation_orchestrator.image_builder import DockerImageBuilder
from automation_orchestrator.models import ImageSpec, PluginManifest
from automation_orchestrator.plugin_registry import PluginRegistry


def test_build_context_contains_plugin_and_deterministic_manifest(tmp_path: Path):
    plugin_root = tmp_path / "plugins"
    source = plugin_root / "example" / "source"
    source.mkdir(parents=True)
    (source / "index.js").write_text("export function apply() {}\n", encoding="utf-8")
    plugin = PluginManifest(
        name="example",
        version="1.2.3",
        description="test",
        npm_package="@automation/dsh-plugin-example",
        entrypoint="index.js",
        inject=["tools"],
        source_dir="source",
    )
    spec = ImageSpec(
        profile="core",
        base_image="automation-dsh-sandbox-core:0.1.0-rc.7",
        image="automation-dsh-sandbox-custom:abc",
        harness_version="0.1.0-rc.7",
        capabilities=["node", "python"],
        plugins=["example"],
        digest="abc",
        requires_build=True,
    )

    context = DockerImageBuilder(PluginRegistry(plugin_root))._build_context(spec, [plugin])

    with tarfile.open(fileobj=context, mode="r") as archive:
        names = archive.getnames()
        dockerfile = archive.extractfile("Dockerfile").read().decode("utf-8")
        manifest = json.load(archive.extractfile("image-manifest.json"))
    assert "plugins/example/index.js" in names
    assert "FROM ${BASE_IMAGE}" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert manifest["image_spec_digest"] == "abc"
    assert manifest["plugins"]["example"]["entrypoint"].endswith(
        "/@automation/dsh-plugin-example/index.js"
    )
