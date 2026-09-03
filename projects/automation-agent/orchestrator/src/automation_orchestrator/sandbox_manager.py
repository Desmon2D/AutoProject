from __future__ import annotations

import base64
import io
import json
import os
import shlex
import tarfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

import docker
from docker.errors import DockerException, NotFound

from .models import AgentImageResolution, SandboxResult


class SandboxExecutionError(RuntimeError):
    pass


class SandboxManager:
    def __init__(
        self,
        jobs_root: Path,
        *,
        max_output_bytes: int = 50 * 1024 * 1024,
        network: str | None = None,
        plugin_networks: dict[str, str] | None = None,
    ):
        self.jobs_root = jobs_root
        self.max_output_bytes = max_output_bytes
        self.network = network
        self.plugin_networks = plugin_networks or {}

    def _client(self):
        return docker.from_env()

    def is_available(self) -> bool:
        try:
            return bool(self._client().ping())
        except DockerException:
            return False

    @staticmethod
    def _environment(
        resolution: AgentImageResolution,
        provider: str = "openai",
    ) -> dict[str, str]:
        # DSH may freely modify the workspace and run Git commands because the
        # outer Docker container is the security boundary: it is non-root,
        # capability-free, read-only and exposes only size-limited tmpfs paths.
        environment: dict[str, str] = {"DSH_PERMISSION_MODE": "danger-full-access"}
        provider_environment = {
            "openai": "OPENAI_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
        }
        credential_name = provider_environment.get(provider)
        if credential_name and os.environ.get(credential_name):
            environment[credential_name] = os.environ[credential_name]
        for plugin in resolution.plugins:
            for name in plugin.required_environment:
                value = os.environ.get(name, "").strip()
                if not value:
                    raise SandboxExecutionError(
                        f"required environment is not configured for plugin {plugin.name}: {name}"
                    )
                environment[name] = value
        return environment

    def run(
        self,
        *,
        execution_id: str,
        task: dict[str, Any],
        resolution: AgentImageResolution,
        skill_files: dict[str, bytes],
    ) -> SandboxResult:
        job_root = self.jobs_root / execution_id
        input_dir = job_root / "input"
        workspace_dir = job_root / "workspace"
        output_dir = job_root / "output"
        for path in (input_dir, workspace_dir, output_dir):
            path.mkdir(parents=True, exist_ok=True)

        task_bytes = json.dumps(task, ensure_ascii=False, indent=2).encode("utf-8")
        (input_dir / "task.json").write_bytes(task_bytes)
        for relative, content in skill_files.items():
            target = workspace_dir.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

        name = f"automation-agent-{execution_id[:40]}-{uuid.uuid4().hex[:8]}"
        container = None
        try:
            client = self._client()
            environment = self._environment(resolution, task.get("provider", "openai"))
            requested_plugins = {plugin.name for plugin in resolution.plugins}
            extra_networks = sorted(
                {
                    network
                    for plugin, network in self.plugin_networks.items()
                    if plugin in requested_plugins and network and network != self.network
                }
            )
            self._ensure_networks(
                client,
                [network for network in [self.network, *extra_networks] if network],
            )
            container_options: dict[str, Any] = {}
            if self.network:
                container_options["network"] = self.network
            container = client.containers.create(
                resolution.image,
                name=name,
                command=["sh", "-lc", "while :; do sleep 3600; done"],
                detach=True,
                user="10001:10001",
                environment=environment,
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                mem_limit="2g",
                nano_cpus=2_000_000_000,
                pids_limit=256,
                tmpfs={
                    "/tmp": "rw,nosuid,nodev,size=128m,uid=10001,gid=10001",
                    "/home/sandbox": "rw,nosuid,nodev,size=256m,uid=10001,gid=10001,mode=0700",
                    "/job/input": "rw,nosuid,nodev,size=16m,uid=10001,gid=10001,mode=0700",
                    "/workspace": "rw,nosuid,nodev,size=512m,uid=10001,gid=10001,mode=0700",
                    "/output": "rw,nosuid,nodev,size=256m,uid=10001,gid=10001,mode=0700",
                },
                labels={"automation.execution_id": execution_id, "automation.role": "agent"},
                **container_options,
            )
            container.start()
            for network_name in extra_networks:
                client.networks.get(network_name).connect(container)
            self._write_files(container, "/job/input", {"task.json": task_bytes})
            self._write_files(container, "/workspace", skill_files)

            readonly = container.exec_run(["chmod", "-R", "a-w", "/job/input"])
            if readonly.exit_code != 0:
                raise SandboxExecutionError("cannot make sandbox input read-only")

            for command in resolution.required_commands:
                probe = container.exec_run(
                    ["sh", "-lc", f"command -v {shlex.quote(command)} >/dev/null"]
                )
                if probe.exit_code != 0:
                    raise SandboxExecutionError(
                        f"resolved image does not provide required command: {command}"
                    )

            execution = container.exec_run(
                ["python3", "/opt/sandbox/runner.py"],
                workdir="/workspace",
                demux=True,
            )
            archived_members = self._copy_output(container, output_dir)
            result_path = output_dir / "result.json"
            if not result_path.is_file():
                stdout, stderr = execution.output or (b"", b"")
                diagnostic = (stderr or stdout or b"").decode("utf-8", errors="replace")[-2000:]
                output_probe = container.exec_run(
                    [
                        "sh",
                        "-lc",
                        "ls -la /output; test ! -f /output/result.json || cat /output/result.json",
                    ]
                )
                probe_text = (output_probe.output or b"").decode("utf-8", errors="replace")[-2000:]
                raise SandboxExecutionError(
                    f"sandbox exited with {execution.exit_code} without result.json: "
                    f"{diagnostic} archive members: {archived_members}; output probe: {probe_text}"
                )
            return SandboxResult.model_validate_json(result_path.read_text(encoding="utf-8"))
        except (DockerException, NotFound) as exc:
            raise SandboxExecutionError(f"Docker execution failed: {exc}") from exc
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except DockerException:
                    pass

    @staticmethod
    def _ensure_networks(client, names: list[str]) -> None:
        for name in names:
            try:
                client.networks.get(name)
            except NotFound:
                client.networks.create(
                    name,
                    driver="bridge",
                    check_duplicate=True,
                    labels={"automation.managed": "true"},
                )

    @staticmethod
    def _write_files(container, root: str, files: dict[str, bytes]) -> None:
        script = (
            "import base64,pathlib,sys; "
            "root=pathlib.Path(sys.argv[1]); "
            "rel=pathlib.PurePosixPath(sys.argv[2]); "
            "target=root.joinpath(*rel.parts); "
            "target.parent.mkdir(parents=True,exist_ok=True); "
            "target.write_bytes(base64.b64decode(sys.argv[3]))"
        )
        for relative, content in sorted(files.items()):
            path = PurePosixPath(relative)
            if path.is_absolute() or ".." in path.parts:
                raise SandboxExecutionError("invalid sandbox input path")
            encoded = base64.b64encode(content).decode("ascii")
            result = container.exec_run(["python3", "-c", script, root, relative, encoded])
            if result.exit_code != 0:
                raise SandboxExecutionError(f"cannot copy file into sandbox: {relative}")

    def _copy_output(self, container, output_dir: Path) -> list[str]:
        packed = container.exec_run(["tar", "-C", "/output", "-cf", "-", "."])
        if packed.exit_code != 0:
            raise SandboxExecutionError("cannot package sandbox output")
        payload = packed.output or b""
        if len(payload) > self.max_output_bytes:
            raise SandboxExecutionError("sandbox output exceeds size limit")

        names: list[str] = []
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
            for member in archive.getmembers():
                names.append(member.name)
                parts = PurePosixPath(member.name).parts
                if parts and parts[0] == "output":
                    parts = parts[1:]
                if not parts:
                    continue
                if member.issym() or member.islnk():
                    raise SandboxExecutionError("sandbox output contains a link")
                target = output_dir.joinpath(*parts)
                resolved = target.resolve()
                if output_dir.resolve() not in resolved.parents:
                    raise SandboxExecutionError("sandbox output contains an invalid path")
                if member.isdir():
                    resolved.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    continue
                resolved.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    continue
                resolved.write_bytes(source.read())
        return names
