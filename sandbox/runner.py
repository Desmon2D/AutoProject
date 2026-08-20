#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any

INPUT_PATH = Path("/job/input/task.json")
OUTPUT_DIR = Path("/output")
WORKSPACE_DIR = Path("/workspace")
DSH_HOME = Path(os.environ.get("DSH_HOME", "/home/sandbox/.dsh"))
AGENT_RESULT_PATH = OUTPUT_DIR / "agent-result.json"
IMAGE_MANIFEST_PATH = Path("/opt/sandbox/image-manifest.json")
GIT_ASKPASS_PATH = Path("/opt/sandbox/git-askpass.py")
GIT_ASKPASS_SOCKET = Path("/tmp/automation-git-askpass.sock")
MODEL_PATTERN = re.compile(r"^[A-Za-z0-9._:/-]{1,200}$")
PLUGIN_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def write_result(payload: dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT_DIR / "result.json.tmp"
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(OUTPUT_DIR / "result.json")


def fail(
    job_id: str,
    code: str,
    message: str,
    *,
    retryable: bool,
    provider: str | None = None,
    model: str | None = None,
    duration_ms: int | None = None,
    artifacts: list[dict[str, str]] | None = None,
) -> int:
    payload: dict[str, Any] = {
        "job_id": job_id,
        "status": "error",
        "summary": message,
        "artifacts": artifacts or [],
        "error": {"code": code, "message": message, "retryable": retryable},
    }
    if provider is not None:
        payload["provider"] = provider
    if model is not None:
        payload["model"] = model
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    write_result(payload)
    return 1


def classify_harness_error(returncode: int, stderr: str) -> tuple[str, str, bool]:
    normalized = stderr.lower()
    if "openai api error (401)" in normalized or "invalid_api_key" in normalized:
        return "PROVIDER_AUTH_ERROR", "Model provider rejected the API credential", False
    if "openai api error (429)" in normalized or "rate_limit" in normalized:
        return "PROVIDER_RATE_LIMIT", "Model provider rate limit was reached", True
    if "transport:" in normalized or "connection error" in normalized:
        return "PROVIDER_TRANSPORT_ERROR", "Cannot reach the model provider API", True
    retryable = returncode not in (2, 64)
    return (
        "HARNESS_EXIT_ERROR",
        f"DeepSeek Harness exited with code {returncode}",
        retryable,
    )


def load_agent_result(path: Path | None = None) -> dict[str, Any]:
    path = path or AGENT_RESULT_PATH
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("agent-result.json must contain an object")
    if result.get("schema_version") != 1:
        raise ValueError("agent-result.json has an unsupported schema_version")
    if result.get("outcome") not in ("SUCCESS", "FAILURE"):
        raise ValueError("agent-result.json outcome must be SUCCESS or FAILURE")
    summary = result.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 4000:
        raise ValueError("agent-result.json summary must contain 1..4000 characters")
    data = result.get("data", {})
    if not isinstance(data, dict):
        raise ValueError("agent-result.json data must be an object")
    artifacts = result.get("artifacts", [])
    if not isinstance(artifacts, list) or len(artifacts) > 100:
        raise ValueError("agent-result.json artifacts must be an array of at most 100 items")
    normalized_artifacts: list[dict[str, str]] = []
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            raise ValueError(f"agent-result.json artifact {index} must be an object")
        artifact_type = artifact.get("type")
        uri = artifact.get("uri")
        if not isinstance(artifact_type, str) or not artifact_type.strip():
            raise ValueError(f"agent-result.json artifact {index} has an invalid type")
        if not isinstance(uri, str) or not uri.strip():
            raise ValueError(f"agent-result.json artifact {index} has an invalid uri")
        normalized = {"type": artifact_type.strip(), "uri": uri.strip()}
        artifact_summary = artifact.get("summary")
        if isinstance(artifact_summary, str) and artifact_summary.strip():
            normalized["summary"] = artifact_summary.strip()
        normalized_artifacts.append(normalized)
    return {
        "outcome": result["outcome"],
        "summary": summary.strip(),
        "data": data,
        "artifacts": normalized_artifacts,
    }


def load_task() -> dict[str, Any]:
    task = json.loads(INPUT_PATH.read_text(encoding="utf-8"))
    if not isinstance(task, dict):
        raise ValueError("task.json must contain an object")
    for field in ("job_id", "prompt", "model"):
        if not isinstance(task.get(field), str) or not task[field].strip():
            raise ValueError(f"{field} must be a non-empty string")
    if task.get("provider", "openai") not in ("openai", "openrouter"):
        raise ValueError("provider must be openai or openrouter")
    if not MODEL_PATTERN.fullmatch(task["model"]):
        raise ValueError("model contains unsupported characters")
    timeout = task.get("timeout_seconds", 600)
    if not isinstance(timeout, int) or not 1 <= timeout <= 3600:
        raise ValueError("timeout_seconds must be an integer from 1 to 3600")
    return task


def configure_provider(provider: str, model: str) -> None:
    credential_name = {
        "openai": "OPENAI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
    }[provider]
    DSH_HOME.mkdir(parents=True, exist_ok=True)
    settings = (
        "llm-pi-ai:\n"
        "  providers:\n"
        f"    {provider}:\n"
        f"      apiKeyEnv: {credential_name}\n"
        "agent-default-model:\n"
        f"  provider: {provider}\n"
        f"  model: {json.dumps(model)}\n"
    )
    (DSH_HOME / "settings.yaml").write_text(settings, encoding="utf-8")


class GitAskpassServer:
    def __init__(self, username: str, token: str, path: Path):
        self.username = username
        self.token = token
        self.path = path
        self._stop = threading.Event()
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.path.unlink(missing_ok=True)
        self._socket.bind(str(self.path))
        self.path.chmod(0o600)
        self._socket.listen(4)
        self._socket.settimeout(0.2)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except (TimeoutError, OSError):
                continue
            with connection:
                prompt = connection.recv(4096).decode("utf-8", errors="replace")
                if "Username" in prompt:
                    response = self.username
                elif "Password" in prompt:
                    response = self.token
                else:
                    response = ""
                connection.sendall(response.encode("utf-8"))

    def close(self) -> None:
        self._stop.set()
        self._socket.close()
        self._thread.join(timeout=1)
        self.path.unlink(missing_ok=True)


def configure_git_auth() -> GitAskpassServer | None:
    token = os.environ.get("GITEA_TOKEN", "").strip()
    if not token:
        return None
    username = os.environ.get("GITEA_USERNAME", "").strip()
    if not username:
        raise ValueError("GITEA_USERNAME is required when GITEA_TOKEN is configured")
    os.environ["GIT_ASKPASS"] = str(GIT_ASKPASS_PATH)
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    os.environ["AUTOMATION_GIT_AUTH_SOCKET"] = str(GIT_ASKPASS_SOCKET)
    return GitAskpassServer(username, token, GIT_ASKPASS_SOCKET)


def build_plugin_patch(
    requested: list[str],
    *,
    manifest_path: Path | None = None,
) -> Path:
    manifest_path = manifest_path or IMAGE_MANIFEST_PATH
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("plugins"), dict):
        raise ValueError("image manifest has an unsupported schema")

    installed = manifest["plugins"]
    selected = list(dict.fromkeys(requested))
    for plugin_name, plugin in installed.items():
        if isinstance(plugin, dict) and plugin.get("mandatory") and plugin_name not in selected:
            selected.insert(0, plugin_name)

    entries: list[dict[str, Any]] = []
    for plugin_name in selected:
        if not isinstance(plugin_name, str) or not PLUGIN_PATTERN.fullmatch(plugin_name):
            raise ValueError("task contains an invalid plugin name")
        plugin = installed.get(plugin_name)
        if not isinstance(plugin, dict):
            raise ValueError(f"plugin is not installed in this image: {plugin_name}")
        entrypoint = plugin.get("entrypoint")
        inject = plugin.get("inject", [])
        config = plugin.get("config", {})
        if not isinstance(entrypoint, str) or not PurePosixPath(entrypoint).is_absolute():
            raise ValueError(f"plugin has an invalid entrypoint: {plugin_name}")
        if not isinstance(inject, list) or not all(isinstance(item, str) for item in inject):
            raise ValueError(f"plugin has invalid injections: {plugin_name}")
        if not isinstance(config, dict):
            raise ValueError(f"plugin has invalid config: {plugin_name}")
        entries.append(
            {
                "id": f"automation-{plugin_name}",
                "name": entrypoint,
                "inject": inject,
                "config": config,
            }
        )

    DSH_HOME.mkdir(parents=True, exist_ok=True)
    patch_path = DSH_HOME / "runtime-plugins.patch.json"
    patch_path.write_text(
        json.dumps([{"insert": entries}], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return patch_path


def main() -> int:
    started = time.time()
    job_id = "unknown"
    try:
        task = load_task()
        job_id = task["job_id"]
    except FileNotFoundError:
        return fail(job_id, "INPUT_NOT_FOUND", f"missing {INPUT_PATH}", retryable=False)
    except (json.JSONDecodeError, ValueError) as exc:
        return fail(job_id, "INVALID_INPUT", str(exc), retryable=False)

    provider = task.get("provider", "openai")
    credential_name = {
        "openai": "OPENAI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
    }[provider]
    if not os.environ.get(credential_name, "").strip():
        return fail(
            job_id,
            "MISSING_CREDENTIAL",
            f"{credential_name} is not set",
            retryable=False,
        )

    configure_provider(provider, task["model"])
    try:
        plugin_patch = build_plugin_patch(task.get("plugins", []))
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        return fail(job_id, "INVALID_IMAGE", str(exc), retryable=False)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    git_auth = configure_git_auth()

    command = [
        "/opt/sandbox/dsh-wrapper.sh",
        "--profile",
        "headless",
        "--patch",
        str(plugin_patch),
        task["prompt"],
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=WORKSPACE_DIR,
            check=False,
            capture_output=True,
            text=True,
            timeout=task.get("timeout_seconds", 600),
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired as exc:
        (OUTPUT_DIR / "stdout.log").write_text(exc.stdout or "", encoding="utf-8")
        (OUTPUT_DIR / "stderr.log").write_text(exc.stderr or "", encoding="utf-8")
        return fail(
            job_id,
            "HARNESS_TIMEOUT",
            "DeepSeek Harness timed out",
            retryable=True,
            provider=provider,
            model=task["model"],
            duration_ms=round((time.time() - started) * 1000),
            artifacts=[
                {"type": "log", "path": "stdout.log"},
                {"type": "log", "path": "stderr.log"},
            ],
        )
    except OSError as exc:
        return fail(job_id, "HARNESS_START_ERROR", str(exc), retryable=True)
    finally:
        if git_auth is not None:
            git_auth.close()

    (OUTPUT_DIR / "stdout.log").write_text(completed.stdout, encoding="utf-8")
    (OUTPUT_DIR / "stderr.log").write_text(completed.stderr, encoding="utf-8")
    duration_ms = round((time.time() - started) * 1000)
    if completed.returncode == 0:
        log_artifacts = [
            {"type": "log", "path": "stdout.log"},
            {"type": "log", "path": "stderr.log"},
        ]
        try:
            agent_result = load_agent_result()
        except FileNotFoundError:
            return fail(
                job_id,
                "AGENT_RESULT_MISSING",
                "Agent finished without calling submit_step_result",
                retryable=True,
                provider=provider,
                model=task["model"],
                duration_ms=duration_ms,
                artifacts=log_artifacts,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            return fail(
                job_id,
                "AGENT_RESULT_INVALID",
                str(exc),
                retryable=False,
                provider=provider,
                model=task["model"],
                duration_ms=duration_ms,
                artifacts=log_artifacts,
            )
        write_result(
            {
                "job_id": job_id,
                "status": "success" if agent_result["outcome"] == "SUCCESS" else "failure",
                "summary": agent_result["summary"],
                "provider": provider,
                "model": task["model"],
                "duration_ms": duration_ms,
                "data": agent_result["data"],
                "artifacts": agent_result["artifacts"] + log_artifacts,
                "error": None,
            }
        )
        return 0

    code, message, retryable = classify_harness_error(
        completed.returncode, completed.stderr
    )
    return fail(
        job_id,
        code,
        message,
        retryable=retryable,
        provider=provider,
        model=task["model"],
        duration_ms=duration_ms,
        artifacts=[
            {"type": "log", "path": "stdout.log"},
            {"type": "log", "path": "stderr.log"},
        ],
    )


if __name__ == "__main__":
    sys.exit(main())
