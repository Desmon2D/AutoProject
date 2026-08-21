import json
import tarfile
from pathlib import Path

from automation_orchestrator.image_builder import DockerImageBuilder
from automation_orchestrator.models import ImageSpec, PluginManifest
from automation_orchestrator.plugin_registry import PluginRegistry


class FakeImage:
    def __init__(self, image_id: str, labels: dict[str, str] | None = None):
        self.id = image_id
        self.labels = labels or {}


class FakeImages:
    def __init__(self, images: dict[str, FakeImage]):
        self.images = images
        self.builds = []

    def get(self, name: str):
        return self.images[name]

    def build(self, **kwargs):
        self.builds.append(kwargs)


class FakeClient:
    def __init__(self, images: FakeImages):
        self.images = images


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


def test_rebuilds_custom_image_when_base_image_changed(tmp_path: Path):
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
        source_dir="source",
    )
    spec = ImageSpec(
        profile="code",
        base_image="automation-code:1",
        image="automation-custom:abc",
        harness_version="1",
        capabilities=["python"],
        plugins=["example"],
        digest="abc",
        requires_build=True,
    )
    images = FakeImages(
        {
            spec.base_image: FakeImage("sha256:new-base"),
            spec.image: FakeImage(
                "sha256:custom",
                {
                    "automation.image-spec": spec.digest,
                    "automation.base-image-id": "sha256:old-base",
                },
            ),
        }
    )
    builder = DockerImageBuilder(
        PluginRegistry(plugin_root), client_factory=lambda: FakeClient(images)
    )

    assert builder.ensure(spec, [plugin]) == spec.image
    assert len(images.builds) == 1
    assert images.builds[0]["labels"]["automation.base-image-id"] == "sha256:new-base"
