# DeepSeek Harness sandbox spike

The image pins the official `@deepseek-ai/dsh` package and runs it as an
unprivileged user. The runner supports the native DeepSeek Harness `openai` and
`openrouter` providers through `OPENAI_API_KEY` and `OPENROUTER_API_KEY`. The
provider and model are selected in `input/task.json`; only the selected
provider's credential is passed into the agent container.

Build the default `code` profile, or both prepared profiles, with:

```powershell
.\sandbox\scripts\build.ps1
.\sandbox\scripts\build-all.ps1
```

Prepared images:

- `automation-dsh-sandbox-core:0.1.0-rc.7` — Harness, Node.js, Python, and the mandatory result plugin;
- `automation-dsh-sandbox-code:0.1.0-rc.7` — the core profile plus Git.
- `automation-dsh-sandbox-delivery:0.1.0-rc.7` — code profile plus tested Gitea and SWIRL plugins.

Each image contains `/opt/sandbox/image-manifest.json`. The runner accepts only
plugins declared by this manifest and creates the Harness patch at runtime.

If Docker Hub DNS is unavailable but the local Node/Hermes base image exists,
the spike can be built through a temporary container with an explicit DNS:

```powershell
.\sandbox\scripts\build-offline.ps1
```

Run the smoke job with:

```powershell
$env:OPENAI_API_KEY = "..."
.\sandbox\scripts\run.ps1
```

The complete OpenRouter smoke check is launched from the repository root:

```powershell
.\scripts\setup\configure-openrouter.ps1
```

`run.ps1` uses Docker's default DNS. Pass `-DnsServer "1.1.1.1"` or a corporate
DNS address only when an explicit override is required.

An API key is supplied only at container runtime and is not written into the
image, task, result, or logs. The job workspace and output survive container
removal in `sandbox/examples/openai-smoke/`.

## Native Step Result plugin

Both images contain the native Cordis plugin
`@automation/dsh-plugin-step-result`. It is marked mandatory in the image
manifest and is attached to the official headless profile for every run.

The plugin registers the model-facing `submit_step_result` tool. Every
successful Harness run must call it exactly once with business outcome
`SUCCESS` or `FAILURE`, a summary, small structured data, and artifact
references. Missing or invalid submission is a technical `ERROR`; `FAILURE`
remains a completed business outcome.
