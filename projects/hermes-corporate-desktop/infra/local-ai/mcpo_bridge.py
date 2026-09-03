from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

from dotenv import dotenv_values
from mcpo.main import run

from mcp_process import safe_environment


def _resolve_command(command: dict[str, str], repo_root: Path) -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    roots = {
        "repository": repo_root,
        "localAppData": Path(local_app_data) if local_app_data else Path("__missing_local_app_data__"),
    }
    root_name = command.get("root", "")
    if root_name not in roots:
        raise RuntimeError(f"Unsupported MCP command root: {root_name!r}")
    return roots[root_name] / Path(command["path"])


def _load_servers(
    manifest_path: Path,
    repo_root: Path,
    secrets_path: Path,
    credentials: dict[str, str | None],
) -> dict[str, dict]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    servers: dict[str, dict] = {}
    skipped: list[str] = []
    launcher = Path(__file__).with_name("mcp_process.py")

    for entry in manifest.get("servers", []):
        server_id = entry["id"]
        executable = _resolve_command(entry["command"], repo_root)
        if not executable.is_file():
            if entry.get("optional", False):
                skipped.append(f"{server_id}: executable not found")
                continue
            raise RuntimeError(f"MCP executable was not found: {executable}")

        missing_env = [
            name
            for name in entry.get("requiredEnv", [])
            if not (credentials.get(name) or "").strip()
        ]
        if missing_env:
            skipped.append(f"{server_id}: missing {', '.join(missing_env)}")
            continue

        credential_names = [
            name
            for name in [*entry.get("requiredEnv", []), *entry.get("optionalEnv", [])]
            if (credentials.get(name) or "").strip()
        ]
        launcher_args = ["--secrets-file", str(secrets_path)]
        for name in credential_names:
            launcher_args.extend(["--secret", name])
        launcher_args.extend(["--command", str(executable), "--", *entry.get("args", [])])

        config: dict[str, object] = {
            "command": sys.executable,
            "args": launcher_args,
        }
        if entry.get("env"):
            config["env"] = entry["env"]
        servers[server_id] = config

    for reason in skipped:
        print(f"Skipping MCP server {reason}", file=sys.stderr)
    if not servers:
        raise RuntimeError("No configured MCP servers are available")
    return servers


def main() -> None:
    api_key = os.environ.get("MCPO_API_KEY", "").strip()
    agent_home_value = os.environ.get("DIT_AGENT_HOME", "").strip()
    port = int(os.environ.get("DIT_MCPO_PORT", "8000"))

    if not api_key:
        raise RuntimeError("MCPO_API_KEY is required")
    if not agent_home_value:
        raise RuntimeError("DIT_AGENT_HOME is required")
    agent_home = Path(agent_home_value).expanduser()
    if not agent_home.is_dir():
        raise RuntimeError("DIT_AGENT_HOME must point to the isolated DIT Agent home")

    secrets_path = agent_home / ".env"
    if not secrets_path.is_file():
        raise RuntimeError(f"DIT Agent secret file was not found: {secrets_path}")
    credentials = dict(dotenv_values(secrets_path))

    repo_root = Path(__file__).resolve().parents[2]
    manifest_path = Path(__file__).with_name("mcp-servers.json")
    servers = _load_servers(manifest_path, repo_root, secrets_path, credentials)

    # mcpo 0.0.20 merges its own complete environment into every stdio child.
    # Keep only OS/network variables in the bridge, then let mcp_process.py add
    # the credentials explicitly declared for one server.
    sanitized_environment = safe_environment()
    os.environ.clear()
    os.environ.update(sanitized_environment)

    with tempfile.TemporaryDirectory(prefix="dit-mcpo-") as temp_dir:
        config_path = Path(temp_dir) / "mcp.json"
        config_path.write_text(
            json.dumps({"mcpServers": servers}, ensure_ascii=False),
            encoding="utf-8",
        )
        asyncio.run(
            run(
                host="0.0.0.0",
                port=port,
                api_key=api_key,
                strict_auth=True,
                cors_allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
                config_path=str(config_path),
                name="DIT Corporate MCPs",
                description="Read-only corporate MCP gateways for DIT Agent",
                version="1.0",
                path_prefix="/",
            )
        )


if __name__ == "__main__":
    main()
