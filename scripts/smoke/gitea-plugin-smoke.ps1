param(
    [string]$Username = "harnes",
    [string]$Repository = "payments-api"
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$envFile = Join-Path $root ".env"
$docker = "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin\docker.exe"
if (-not (Test-Path -LiteralPath $envFile)) {
    throw "Run scripts/dev/start-dev-gitea.ps1 first"
}

$orchestratorReady = $false
for ($attempt = 1; $attempt -le 30; $attempt++) {
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:8080/health" -TimeoutSec 2 | Out-Null
        $orchestratorReady = $true
        break
    }
    catch {
        Start-Sleep -Seconds 1
    }
}
if (-not $orchestratorReady) {
    throw "Orchestrator did not become ready at http://127.0.0.1:8080"
}

$payload = @{
    execution_id = "gitea-plugin-smoke"
    workflow_id = "gitea-plugin-smoke"
    step = @{
        id = "inspect-repository"
        prompt = "Inspect the configured development repository"
        plugins = @("gitea")
        skills = @("git")
        provider = "openai"
        model = "smoke-model"
        timeout_seconds = 60
    }
    context = @{}
} | ConvertTo-Json -Depth 8
$prepared = Invoke-RestMethod `
    -Method Post `
    -Uri "http://127.0.0.1:8080/v1/agent-steps/prepare" `
    -ContentType "application/json" `
    -Body $payload

& $docker run --rm `
    --network automation-agent-source `
    --env-file $envFile `
    --env "GITEA_USERNAME=$Username" `
    --env "GITEA_REPOSITORY=$Repository" `
    --volume "${PSScriptRoot}:/smoke:ro" `
    --entrypoint node `
    $prepared.image `
    /smoke/gitea-plugin-smoke.mjs
if ($LASTEXITCODE -ne 0) { throw "Gitea Harness plugin smoke check failed" }
