param(
    [string]$Model = "openai/gpt-4.1-nano",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$docker = "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin\docker.exe"
$envFile = Join-Path $root ".env"
if (-not (Test-Path -LiteralPath $docker)) { throw "Docker CLI not found: $docker" }
if (-not (Test-Path -LiteralPath $envFile)) {
    throw "Run scripts/configure-openrouter.ps1 first"
}
if (-not ([IO.File]::ReadAllLines($envFile) | Where-Object {
    $_.StartsWith("OPENROUTER_API_KEY=", [StringComparison]::Ordinal) -and
    $_.Length -gt "OPENROUTER_API_KEY=".Length
})) {
    throw "OPENROUTER_API_KEY is not configured in .env"
}

Push-Location $root
try {
    if (-not $SkipBuild) {
        & .\sandbox\scripts\build-all.ps1
        if ($LASTEXITCODE -ne 0) { throw "Sandbox image build failed" }
        & $docker compose build orchestrator
        if ($LASTEXITCODE -ne 0) { throw "Orchestrator build failed" }
    }
    & $docker compose up -d --force-recreate orchestrator worker dashboard
    if ($LASTEXITCODE -ne 0) { throw "Cannot start the OpenRouter test stack" }

    $health = $null
    for ($attempt = 1; $attempt -le 30; $attempt++) {
        try {
            $health = Invoke-RestMethod "http://127.0.0.1:8080/health" -TimeoutSec 2
            break
        }
        catch { Start-Sleep -Seconds 1 }
    }
    if ($null -eq $health -or -not $health.providers.openrouter.configured) {
        throw "Orchestrator started without OPENROUTER_API_KEY"
    }

    $executionId = "openrouter-smoke-" + [Guid]::NewGuid().ToString("N")
    $payload = @{
        execution_id = $executionId
        workflow_id = "openrouter-connectivity"
        iteration = 1
        attempt = 1
        step = @{
            id = "openrouter-smoke"
            prompt = "Verify the DeepSeek Harness OpenRouter connection. Call submit_step_result exactly once with outcome SUCCESS, a short summary, data containing connectivity=true, and no artifacts."
            plugins = @()
            skills = @()
            provider = "openrouter"
            model = $Model
            timeout_seconds = 180
        }
        context = @{}
    } | ConvertTo-Json -Depth 10
    $result = Invoke-RestMethod `
        -Method Post `
        -Uri "http://127.0.0.1:8080/v1/agent-steps/run" `
        -ContentType "application/json" `
        -Body $payload `
        -TimeoutSec 240
    if ($result.execution_status -ne "COMPLETED" -or $result.outcome -ne "SUCCESS") {
        throw "OpenRouter Harness smoke check failed"
    }

    [pscustomobject]@{
        Provider = $result.data.provider
        Model = $Model
        Execution = $result.execution_id
        Status = $result.execution_status
        Outcome = $result.outcome
        Connectivity = $result.data.connectivity
    } | Format-List
}
finally {
    Pop-Location
}
