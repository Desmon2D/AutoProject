# Orchestrator API prototype

## Local Python environment

The project pins Python in `.python-version` and uses a Python installation managed by `uv`, so
the virtual environment does not depend on a separately installed system Python. From this
directory, create or restore the environment with:

```powershell
uv python install
uv sync --extra dev
```

Run checks through `uv run`, for example `uv run pytest -m "not docker"` and
`uv run ruff check .`. Do not create `.venv` with an unpinned system Python.

This module is the first vertical slice of the modular monolith:

1. receive an Agent Step through HTTP;
2. build bounded workflow context;
3. resolve the plugins requested by the scenario;
4. derive required capabilities and select the smallest prepared image;
5. build and cache a deterministic custom image when an allowed plugin is absent;
6. prepare the required tools inside the workspace;
7. execute the sandbox image through the Docker Engine;
8. normalize `/output/result.json` into an internal Step Result.

Run from the repository root:

```powershell
.\scripts\dev\start-low-memory.ps1
Invoke-RestMethod http://localhost:8080/health
```

### Low-memory development mode

On a memory-constrained workstation, use the lightweight stack instead of keeping Gitea, full
SWIRL, and Redis running:

```powershell
.\scripts\dev\start-low-memory.ps1
```

It runs the orchestrator, worker, dashboard, and an 18–25 MiB SWIRL-compatible local search
service backed by editable files in `infra/swirl/lite-data/`. On the current development machine the
whole stack uses about 200 MiB of container memory. Add local Gitea only when needed with
`-WithGitea`, or use `-CoreOnly` to disable search as well.

The lightweight service preserves the SWIRL HTTP contract for development but does not replace
full federated search. Run `scripts/dev/start-dev-swirl.ps1` only for a dedicated full integration
check; return to the lightweight mode by running `scripts/dev/start-low-memory.ps1` again.

The operations dashboard is available at `http://127.0.0.1:4173`. It refreshes
health, workflow, scenario, plugin, skill, and image data every ten seconds.

## Model providers

The default development provider is native DeepSeek Harness `openrouter` with
`z-ai/glm-4.7-flash`. The model is inexpensive and advertises tool calling,
which is required by the native `step-result` plugin.

The `implement-ticket` scenario uses `openai/gpt-4.1`: repository editing,
Git operations, and pull-request creation require stronger tool use than the
default event-summary tasks.

Configure a key and run a real end-to-end check through the orchestrator,
DeepSeek Harness, OpenRouter, and the plugin with:

```powershell
.\scripts\setup\configure-openrouter.ps1
```

The key prompt is hidden. The script validates the key and model against
OpenRouter, stores the key only in the Git-ignored local `.env`, recreates the
services, and executes a minimal Harness task. To configure without running the
smoke check, pass `-SkipSmoke`; to reuse already built images, pass `-SkipBuild`.

OpenAI remains available as a second native provider. Its standalone smoke check
is:

```powershell
.\scripts\smoke\openai-smoke.ps1
```

The `/health` response reports only whether each provider credential is
configured, never its value, plus the current default provider and model.

OpenAPI is available at `http://localhost:8080/docs`.

## Operation journal

State changes are written to the append-only SQLite operation journal. Its
database triggers reject updates and deletion, while credential-like fields are redacted before
storage.

Main endpoints:

- `POST /v1/webhooks/plane` — signed Plane issue ingestion;
- `GET /v1/workflows` — workflow history ordered by last update;
- `POST /v1/webhooks/gitea` — signed Gitea event ingestion;
- `GET /v1/plugins` — plugin catalog and availability;
- `GET /v1/skills` — filesystem skill catalog and runtime requirements;
- `GET /v1/capabilities` — available runtime capabilities;
- `GET /v1/images` — prepared sandbox image profiles;
- `GET /v1/scenarios` — validated workflow graph catalog;
- `POST /v1/triggers` — idempotently create and enqueue a workflow (`202 Accepted`);
- `GET /v1/workflows/{workflow_id}` — persisted workflow state and execution history;
- `POST /v1/workflows/{workflow_id}/review` — resolve and re-enqueue a waiting review;
- `POST /v1/workflows/{workflow_id}/cancel` — cancel pending/waiting work and prevent a late
  worker result from reviving it;
- `POST /v1/workflows/{workflow_id}/retry` — re-enqueue a technically failed workflow with a new
  overall deadline;
- `GET /v1/audit-events` — filtered, paginated operation journal;
- `POST /v1/context/build` — bounded context preview;
- `POST /v1/agent-steps/prepare` — resolved image, plugins, and sandbox task;
- `POST /v1/agent-steps/run` — synchronous Agent Step execution;
- `GET /v1/agent-steps/{execution_id}` — persisted normalized result;
- `GET /v1/agent-steps/{execution_id}/artifacts/{path}` — persisted output artifact.
- `GET /v1/artifacts?execution_id=...` — artifact metadata, digest, size, and expiry.
- `POST /v1/artifacts/cleanup` — remove expired registered artifacts and journal the result.

Repeated `run` requests with the same `execution_id` and body return the stored result.
Using the same identifier for a different request returns HTTP `409`.

`step-result` is a mandatory native Cordis plugin and is added to every Agent Step.
Scenarios request every required tool through `step.plugins`. The resolver maps `git` and
`python` to the corresponding prepared image capabilities internally.

The prepared `core` and `code` images are described in `images/*.json`. Image
selection is deterministic: plugin and skill requirements produce an `ImageSpec`
digest. A custom plugin must be enabled, have a local `source_dir`, and declare
its capabilities; custom images are tagged by that digest and reused from Docker's cache.

## Gitea plugin

The enabled native `gitea` plugin provides repository and pull-request API tools.
An Agent Step requesting it selects the `code` profile and produces a cached custom
image. Set `GITEA_BASE_URL`, `GITEA_USERNAME`, and `GITEA_TOKEN` in the orchestrator
environment; these variables are allowlisted into a sandbox only when the plugin is
selected. The URL must be reachable from the agent container.

### Local development Gitea

This workspace reuses the existing rootless `gitea/gitea:1.24.5-rootless` image
and the external `project_gitea-data` and `project_gitea-config` volumes. Start it
and configure the scoped orchestrator token with:

```powershell
.\scripts\dev\start-dev-gitea.ps1
```

The command verifies a real clone and push to the persistent
`automation-connectivity` branch in `harnes/payments-api`. It stores the token in
the ignored `.env` file and recreates the orchestrator and worker. Gitea is
available to the host at
`http://127.0.0.1:3000` and to sandboxes at `http://gitea:3000` through the
`automation-agent-source` Docker network.

Verify the native plugin from a generated DeepSeek Harness image with:

```powershell
.\scripts\smoke\gitea-plugin-smoke.ps1
```

The external volumes are a deliberate dependency of this development workspace.
On a new machine, restore or create equivalent rootless Gitea volumes first.

`start-dev-gitea.ps1` also creates or updates the repository push webhook. Its
secret is generated once, stored in the ignored `.env`, and passed to the
orchestrator. Deliveries are verified against `X-Gitea-Signature` before JSON is
parsed. The webhook returns `202 Accepted` after persisting an idempotent workflow
and enqueueing its identifier. The separate `worker` container then runs the agent,
so Gitea and the dashboard are not blocked by model latency.

The `gitea-push` scenario uses OpenRouter `openai/gpt-4.1-nano` to summarize ordinary
normalized push events through the mandatory `step-result` tool. Pushes to
`refs/heads/automation/*` are ignored because their parent workflow already tracks them; this
prevents duplicate dashboard entries and model calls. Commit messages are explicitly treated as
untrusted data.

The queue is a SQLite database in the shared persistent `/data` volume. A worker
claims one workflow with a renewable lease. After a worker crash the lease expires
and another worker can safely resume the same workflow; deterministic execution IDs
reuse any Step Result that was already persisted. `/health` reports queue counts
and whether a worker heartbeat is current.

A review decision received while the previous queue lease is still closing records a
transactional requeue request instead of losing the continuation. The worker also reconciles
runnable workflow files with queue state every 60 seconds by default. Missing or prematurely
settled queue jobs are restored, while exhausted queue retries mark the workflow `FAILED` with
`WORKFLOW_QUEUE_FAILED` for an explicit manual retry.

The same worker performs artifact cleanup at startup and then every
`ARTIFACT_CLEANUP_INTERVAL_SECONDS` (one hour by default). Only expired registered files below an
execution's `output/` directory are removed. Requests and structured step results remain intact.
File operations are behind an artifact-storage interface; the current implementation is local,
while an object-storage implementation can be added without changing the registry or API.

## Workflow engine

Scenario files in `scenarios/*.json` are validated as directed graphs. Trigger
delivery is idempotent by scenario, source, event, and external event ID. The engine
persists workflow state, step iterations, retry attempts, review comments, and
normalized Step Results under `/data/jobs/workflows`.

Each step attempt persists its lifecycle (`PENDING`, `READY`, `RUNNING`, `WAITING`,
`COMPLETED`, `ERROR`, or `CANCELLED`) together with a timestamped status history. A technical
`ERROR` can schedule another attempt according to the scenario retry policy. The worker returns
the job to the durable queue with `available_at`, so the delay does not block a thread or keep a
queue lease. Retries keep the same logical iteration and receive a new attempt number. Exhausting
the policy moves the workflow to `FAILED`; business `FAILURE` follows the scenario transition and
is not retried automatically.

A completed workflow exposes its terminal business `outcome` separately from technical `status`.
The implementation failure route ends as `COMPLETED / FAILURE`; a successfully pushed
implementation branch ends as `COMPLETED / SUCCESS` and is ready for the testing workflow.

Command Steps use an explicit allowlist; arbitrary shell commands are not accepted. Review Steps
can wait either for an intermediate review decision or for the pull request to be merged or closed.
They pause in `WAITING` without retaining a worker or agent container. Agent Steps reuse the same Context Builder,
Image Resolver, sandbox, and result contract as the standalone agent API.

Each scenario has an overall `timeout_seconds` (one day by default). The remaining workflow time
also bounds the next agent sandbox timeout. Cancellation is persisted as a separate marker before
the workflow state is updated, so a concurrently finishing worker cannot overwrite it. Manual
retry is allowed only from `FAILED` and creates a fresh overall deadline.

The orchestrator is trusted infrastructure and needs access to the Docker socket.
Agent containers do not receive that socket.

## Ticket implementation workflow

The `plane.issue.ready_for_development` trigger starts the `implement-ticket` scenario. The
signed Plane webhook normalizes a ready issue and maps its project to an allowed Gitea repository.
The scenario performs a bounded SWIRL context search and runs the delivery agent with Git, Gitea,
and SWIRL. The agent pushes `automation/<workflow_id>` and returns its exact commit without
creating a pull request. The workflow records `plane_recommendation=move_to_testing`.

## Ticket testing workflow

The `plane.issue.testing` trigger starts the `test-ticket` scenario. The repository is always
derived from the allowed Plane project mapping and must not be duplicated in the issue
description. When the same issue previously completed `implement-ticket`, the orchestrator
automatically supplies that workflow's exact branch and commit. For a standalone testing issue
without a preceding implementation workflow, add two structured Plane links titled
`Рабочая ветка: branch` and `Коммит реализации: full-40-character-hash`. The legacy
`Automation implementation ref: branch` description marker remains supported for older tasks.
Its two agent roles are strictly separated:

1. `write-tests` reads the Plane requirements and the implementation at the supplied exact
   commit when available, writes only test code, and pushes one stable `automation/<workflow_id>` branch without
   executing tests or creating a pull request;
2. `execute-tests` checks out the exact commit returned by the author, cannot edit repository
   files, runs the project test command, and returns a structured `PASSED`, `PRODUCT_FAILURE`, or
   `TEST_CODE_ERROR` report;
3. after `PASSED`, the orchestrator creates one final pull request from that exact tested branch
   to the repository default branch and enters `WAITING`;
4. merging the pull request completes the workflow successfully, while closing it without merge
   completes the workflow with a business failure. Review approval alone does not finish this
   final wait.

Invalid test code may return to the author once. No pull request is created for invalid tests or a
product failure. Every final result is written to the Plane issue as an idempotent comment. A
product failure returns the issue to `Ready for development`, a merged final pull moves it to the
configured completed state, and a pull closed without merge moves it to the configured cancelled
state. A successful implementation records its exact branch and commit but leaves the state
unchanged so that the user controls when testing starts.

The Community edition used locally does not expose arbitrary custom work-item fields. The branch
and commit therefore use Plane's built-in Links section as two separately visible structured
values. Their URLs open the exact Gitea branch and commit. When an external developer supplies
both links and moves the issue to `Testing`, the testing workflow reads them through the Plane API.

Configure `PLANE_TESTING_STATE_IDS` or `PLANE_TESTING_STATE_NAMES` (the local default is
`Testing`). For a local trigger, create a Plane issue directly in that state with:

```powershell
.\scripts\dev\submit-dev-ticket.ps1 -StateName Testing -ImplementationRef "feature/payment-reference" -Title "Test payment reference validation" -Description "Write automated tests for the implemented payment reference rules."
```

For local development set `PLANE_WEBHOOK_SECRET`, `PLANE_READY_STATE_IDS` or
`PLANE_READY_STATE_NAMES`, and `PLANE_PROJECT_REPOSITORIES`. The last value is a JSON object whose
keys are Plane project identifiers or IDs and whose values are Gitea repositories in `owner/name`
format. Repeated deliveries for the same issue revision reuse the existing workflow.

Writing workflow results back requires `PLANE_API_TOKEN`, `PLANE_WORKSPACE_SLUG`,
`PLANE_COMPLETED_STATE_IDS`, and `PLANE_CANCELLED_STATE_IDS`. The local Plane setup script creates
the token and state mappings and passes them to both the orchestrator and worker. Comment external
IDs contain the workflow and result, so a safe retry does not create duplicate comments.

Gitea write operations require `GITEA_ALLOWED_REPOSITORIES` as a comma-separated allowlist.
PR and comment tools require `workflow_id` and a stable `idempotency_key` and reuse an existing
object carrying the same hidden marker.

Plane, BookStack, and SWIRL are not required for normal contract and workflow development. Their
HTTP contracts are covered with fixtures and tests, and each integration is checked separately
only when needed. This avoids keeping the heavy Plane container and the SWIRL NLP processes in
memory together. SWIRL is an opt-in Compose profile so normal development does not consume memory for its NLP
processes. The core orchestrator starts without a SWIRL client; unit tests and response fixtures
cover the integration contract. When a live check is needed, the local `harnes-swirl:4.5.0.7`
image runs with dedicated Redis and persistent `swirl-data` / `swirl-redis-data` volumes. The old
`project_swirl-*` volumes are intentionally not reused. Start SWIRL, recreate the orchestrator and
worker with its connection settings, and verify the real Search API with:

```powershell
.\scripts\dev\start-dev-swirl.ps1
```

The UI is available at `http://localhost:8083/galaxy/`. Override `SWIRL_IMAGE`,
`SWIRL_USERNAME`, and `SWIRL_PASSWORD` in the ignored `.env` when needed. The orchestrator uses
the internal `http://swirl:8000` URL and synchronous `GET /api/swirl/search/?qs=...`, then limits
normalized excerpts before adding them to agent context. SWIRL result text is always marked as
untrusted reference data.

The development default keeps the optional Celery beat scheduler disabled to avoid loading a
third copy of SWIRL's NLP stack. Set `SWIRL_ENABLE_BEAT=true` only when testing scheduled searches
or subscriptions; ordinary federated search uses the always-on Celery worker.

BookStack is the local Confluence replacement and remains behind SWIRL. When
`BOOKSTACK_BASE_URL`, `BOOKSTACK_TOKEN_ID`, and `BOOKSTACK_TOKEN_SECRET` are configured together,
the SWIRL startup script idempotently creates or updates the non-default `Local BookStack` search
provider tagged `bookstack`. The implementation scenario limits its automatic context search to
that tag. BookStack credentials are passed only to SWIRL, never to the orchestrator or agent
container. Normal development uses the stored response fixture and does not start either service.

Return to the low-memory core mode with:

```powershell
docker compose stop swirl swirl-redis
docker compose up -d --force-recreate orchestrator worker
```

The prepared `delivery` image contains the Gitea and SWIRL native plugins. Agent containers use
a dedicated model network; the Gitea and SWIRL networks are attached only when their respective
plugins are requested. Redis remains isolated on `automation-swirl-backend` and is not reachable
from agent containers.

After configuring OpenRouter, Gitea, and SWIRL, launch a live smoke workflow from the repository
root:

```powershell
.\scripts\smoke\implement-ticket-smoke.ps1 -TicketId A-1 -Summary "Fix payment retry"
```

Compose publishes the API only on `127.0.0.1` for the local client.
