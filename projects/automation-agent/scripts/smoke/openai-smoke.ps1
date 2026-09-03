param(
    [string]$Model = "gpt-5.6-terra",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$docker = "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin\docker.exe"
if (-not (Test-Path -LiteralPath $docker)) {
    throw "Docker CLI not found: $docker"
}

$temporaryKey = $false
$secretPointer = [IntPtr]::Zero
if ([string]::IsNullOrWhiteSpace($env:OPENAI_API_KEY) -and
    -not (Test-Path -LiteralPath (Join-Path $repositoryRoot ".env"))) {
    $secureKey = Read-Host "OpenAI API key" -AsSecureString
    try {
        $secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
        $env:OPENAI_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer)
    }
    finally {
        if ($secretPointer -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer)
        }
    }
    if ([string]::IsNullOrWhiteSpace($env:OPENAI_API_KEY)) {
        throw "OPENAI_API_KEY cannot be empty"
    }
    $temporaryKey = $true
}

Push-Location $repositoryRoot
try {
    $composeArguments = @("compose", "up", "-d")
    if (-not $SkipBuild) {
        $composeArguments += "--build"
    }
    $composeArguments += "orchestrator"
    & $docker @composeArguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose up failed"
    }

    $health = $null
    for ($attempt = 1; $attempt -le 30; $attempt++) {
        try {
            $health = Invoke-RestMethod -Uri "http://localhost:8080/health" -TimeoutSec 2
            break
        }
        catch {
            Start-Sleep -Seconds 1
        }
    }
    if ($null -eq $health) {
        throw "Orchestrator did not become healthy"
    }
    if (-not $health.providers.openai.configured) {
        throw "Orchestrator started without OPENAI_API_KEY"
    }

    $executionId = "openai-smoke-" + [Guid]::NewGuid().ToString("N")
    $payload = @{
        execution_id = $executionId
        workflow_id = "openai-connectivity"
        iteration = 1
        attempt = 1
        step = @{
            id = "openai-smoke"
            prompt = "Confirm that the OpenAI-backed DeepSeek Harness agent is running. Call submit_step_result exactly once with outcome SUCCESS, a short summary, data containing connectivity=true, and no artifacts."
            plugins = @()
            provider = "openai"
            model = $Model
            timeout_seconds = 180
        }
        context = @{}
    } | ConvertTo-Json -Depth 10

    $result = Invoke-RestMethod `
        -Method Post `
        -Uri "http://localhost:8080/v1/agent-steps/run" `
        -ContentType "application/json" `
        -Body $payload `
        -TimeoutSec 240
    $result | ConvertTo-Json -Depth 10

    if ($result.execution_status -ne "COMPLETED" -or $result.outcome -ne "SUCCESS") {
        throw "OpenAI Harness smoke check failed"
    }
}
finally {
    Pop-Location
    if ($temporaryKey) {
        Remove-Item Env:\OPENAI_API_KEY -ErrorAction SilentlyContinue
    }
}

