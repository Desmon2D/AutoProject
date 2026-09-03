# Local OpenWebUI with DIT corporate MCPs

Run from the repository root:

```powershell
.\infra\local-ai\start-openwebui.ps1
```

The script:

- starts Git, Outlook, Staff, CFC, Jira, and Confluence MCPs through one authenticated MCPO bridge;
- reuses `DIT_GIT_TOKEN`, `DIT_JIRA_TOKEN`, and `DIT_CONFLUENCE_TOKEN` from the isolated DIT Agent home;
- uses the existing Windows Credential Manager sessions for Outlook, Staff, and CFC;
- starts a Docker LiteLLM gateway and routes OpenWebUI to OpenRouter through it;
- registers the MCPO OpenAPI endpoint in the OpenWebUI environment;
- synchronizes the persistent OpenWebUI tool-server setting without changing users or chats;
- starts the `litellm` and `openwebui` Compose services.

## LiteLLM gateway

LiteLLM runs in the `hermes-corporate-litellm` Docker container and is published
only on `127.0.0.1:4000`. It exposes an
OpenAI-compatible API protected by a generated `LITELLM_MASTER_KEY` and forwards
`deepseek/deepseek-v4-flash` to OpenRouter. Only the gateway receives the
OpenRouter key. Inside Docker, the local v2rayN proxy address is translated from
`127.0.0.1:10808` to `host.docker.internal:10808`.

The Admin UI is available at <http://localhost:4000/ui/>. Its generated login
is stored as `UI_USERNAME` and `UI_PASSWORD` in the isolated DIT Agent `.env`.
The Compose stack includes a private PostgreSQL container for persistent UI
settings, virtual keys, budgets, and usage data; the database is not published
to the host.

Start it independently and configure the isolated DIT Agent to use it:

```powershell
.\infra\local-ai\start-litellm.ps1 -ConfigureHermes
```

Stop it with:

```powershell
.\infra\local-ai\stop-litellm.ps1
```

The first configuration creates
`%LOCALAPPDATA%\hermes-corporate-dev\agent-home\config.yaml.before-litellm`.
DIT Agent then uses `http://127.0.0.1:4000/v1`; OpenWebUI uses the same host
gateway through the internal Compose address `http://litellm:4000/v1`.

The launcher creates `litellm.docker.env` next to the isolated DIT Agent `.env`,
outside the repository, from the credentials already stored there. If v2rayN
does not accept connections from Docker, enable its local/LAN
inbound for port `10808`; TUN mode is not required.

The server list, commands, corporate endpoints, and OpenWebUI labels have one
source of truth: [`mcp-servers.json`](mcp-servers.json). Add or change a server
there instead of editing both the Python bridge and PowerShell launcher.

Before the first run, install the five MCP packages shipped by this repository:

```powershell
uv pip install --python .venv\Scripts\python.exe `
  -e services\dit-git-mcp `
  -e services\dit-staff-mcp `
  -e services\dit-cfc-mcp `
  -e services\dit-jira-mcp `
  -e services\dit-confluence-mcp
```

Copy the three UI/MCP `*.env.example` templates to their corresponding ignored
`.env.*` files and replace placeholders locally. Corporate API tokens continue
to live in the isolated DIT Agent home. A server whose required token is absent
is skipped with a warning; a missing in-repository executable remains a setup
error. Outlook is explicitly optional because its source is currently external
to this repository.

```powershell
Copy-Item infra\local-ai\hermes.env.example infra\local-ai\.env.hermes
Copy-Item infra\local-ai\mcpo.env.example infra\local-ai\.env.mcpo
Copy-Item infra\local-ai\openwebui.env.example infra\local-ai\.env.openwebui
```

MCPO 0.0.20 normally copies its complete process environment to every child.
The local bridge therefore removes unrelated credentials before MCPO starts and
launches each MCP through `mcp_process.py`, which adds only the credential names
declared for that server in the manifest. This prevents accidental cross-service
token inheritance; it is not an OS sandbox against malicious code running as the
same Windows user.

OpenWebUI is available at <http://localhost:3000>. MCPO listens on port `8000`; each MCP is mounted under its own path (`/dit_git`, `/dit_outlook`, `/dit_staff`, `/dit_cfc`, `/dit_jira`, `/dit_confluence`) and requires one generated bearer key. The MCPO key and OpenRouter credential are stored in ignored local environment files. Corporate tokens remain only in the isolated DIT Agent home and are not copied.

Stop both processes with:

```powershell
.\infra\local-ai\stop-openwebui.ps1
```

If a different isolated DIT Agent home is used:

```powershell
.\infra\local-ai\start-openwebui.ps1 -DitAgentHome 'C:\path\to\agent-home'
```
