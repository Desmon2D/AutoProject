from __future__ import annotations

import io
import json
import re
import tarfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import docker
from docker.errors import BuildError, DockerException, ImageNotFound

from .models import ImageSpec, PluginManifest
from .plugin_registry import PluginRegistry, PluginResolutionError

NPM_PACKAGE_PATTERN = re.compile(r"^(?:@[a-z0-9._-]+/)?[a-z0-9._-]+$")


class ImageBuildError(RuntimeError):
    pass


class DockerImageBuilder:
    def __init__(
        self,
        plugin_registry: PluginRegistry,
        *,
        client_factory: Callable[[], object] = docker.from_env,
    ):
        self.plugin_registry = plugin_registry
        self.client_factory = client_factory

    def ensure(self, spec: ImageSpec, plugins: list[PluginManifest]) -> str:
        if not spec.requires_build:
            return spec.image
        client = self.client_factory()
        try:
            base_image = client.images.get(spec.base_image)
            base_image_id = base_image.id
        except (ImageNotFound, DockerException) as exc:
            raise ImageBuildError(
                f"cannot inspect base sandbox image {spec.base_image}: {exc}"
            ) from exc
        try:
            current = client.images.get(spec.image)
            labels = current.labels or {}
            if (
                labels.get("automation.image-spec") == spec.digest
                and labels.get("automation.base-image-id") == base_image_id
            ):
                return spec.image
        except ImageNotFound:
            pass
        except DockerException as exc:
            raise ImageBuildError(f"cannot inspect sandbox image: {exc}") from exc

        try:
            context = self._build_context(spec, plugins)
            client.images.build(
                fileobj=context,
                custom_context=True,
                tag=spec.image,
                buildargs={"BASE_IMAGE": spec.base_image},
                labels={
                    "automation.image-spec": spec.digest,
                    "automation.base-image-id": base_image_id,
                    "automation.sandbox-profile": spec.profile,
                },
                rm=True,
                forcerm=True,
            )
            return spec.image
        except (BuildError, DockerException, OSError, PluginResolutionError) as exc:
            raise ImageBuildError(f"cannot build sandbox image {spec.image}: {exc}") from exc

    def _build_context(self, spec: ImageSpec, plugins: list[PluginManifest]) -> io.BytesIO:
        plugin_entries: dict[str, dict[str, object]] = {}
        docker_lines = ["ARG BASE_IMAGE", "FROM ${BASE_IMAGE}", "USER root"]
        sources: list[tuple[str, Path]] = []

        for plugin in sorted(plugins, key=lambda item: item.name):
            if not NPM_PACKAGE_PATTERN.fullmatch(plugin.npm_package):
                raise ImageBuildError(f"plugin {plugin.name} has an invalid npm package")
            entrypoint = PurePosixPath(plugin.entrypoint)
            if entrypoint.is_absolute() or ".." in entrypoint.parts:
                raise ImageBuildError(f"plugin {plugin.name} has an invalid entrypoint")
            install_root = (
                PurePosixPath("/usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules")
                / plugin.npm_package
            )
            absolute_entrypoint = install_root / entrypoint
            plugin_entries[plugin.name] = {
                "version": plugin.version,
                "entrypoint": str(absolute_entrypoint),
                "inject": plugin.inject,
                "config": plugin.config,
                "mandatory": plugin.mandatory,
            }
            if not plugin.built_into_image:
                source = self.plugin_registry.source(plugin)
                archive_path = f"plugins/{plugin.name}"
                sources.append((archive_path, source))
                docker_lines.append(f"COPY {archive_path}/ {install_root}/")
                docker_lines.append(f"RUN node -e \"import('file://{absolute_entrypoint}')\"")

        image_manifest = {
            "schema_version": 1,
            "profile": spec.profile,
            "harness_version": spec.harness_version,
            "capabilities": spec.capabilities,
            "image_spec_digest": spec.digest,
            "plugins": plugin_entries,
        }
        docker_lines.extend(
            [
                "COPY image-manifest.json /opt/sandbox/image-manifest.json",
                f'LABEL automation.image-spec="{spec.digest}"',
                "USER 10001:10001",
            ]
        )

        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w") as archive:
            self._add_bytes(archive, "Dockerfile", "\n".join(docker_lines).encode("utf-8"))
            self._add_bytes(
                archive,
                "image-manifest.json",
                json.dumps(image_manifest, sort_keys=True, indent=2).encode("utf-8"),
            )
            for archive_path, source in sources:
                for path in sorted(source.rglob("*")):
                    if not path.is_file():
                        continue
                    relative = path.relative_to(source).as_posix()
                    self._add_bytes(
                        archive,
                        f"{archive_path}/{relative}",
                        path.read_bytes(),
                    )
        payload.seek(0)
        return payload

    @staticmethod
    def _add_bytes(archive: tarfile.TarFile, name: str, content: bytes) -> None:
        info = tarfile.TarInfo(name)
        info.size = len(content)
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(content))
