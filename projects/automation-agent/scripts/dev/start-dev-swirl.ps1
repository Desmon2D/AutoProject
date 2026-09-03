param(
    [string]$Query = "automation healthcheck",
    [int]$TimeoutSeconds = 300,
    [switch]$SkipStart
)

$ErrorActionPreference = "Stop"
$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $repositoryRoot

$dockerCandidates = @(
    (Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\resources\bin\docker.exe"),
    "docker"
)
$docker = $dockerCandidates | Where-Object {
    if ($_ -eq "docker") {
        return [bool](Get-Command docker -ErrorAction SilentlyContinue)
    }
    return Test-Path -LiteralPath $_
} | Select-Object -First 1

if (-not $docker) {
    throw "Docker CLI was not found."
}

function Get-ConfiguredValue {
    param(
        [string]$Name,
        [string]$DefaultValue
    )

    $processValue = [Environment]::GetEnvironmentVariable($Name)
    if (-not [string]::IsNullOrWhiteSpace($processValue)) {
        return $processValue
    }

    if (Test-Path -LiteralPath ".env") {
        $line = Get-Content -LiteralPath ".env" | Where-Object {
            $_ -match "^$([regex]::Escape($Name))="
        } | Select-Object -Last 1
        if ($line) {
            $fileValue = ($line -split "=", 2)[1].Trim()
            if (-not [string]::IsNullOrWhiteSpace($fileValue)) {
                return $fileValue
            }
        }
    }

    return $DefaultValue
}

function Set-DotEnvValue {
    param(
        [string]$Name,
        [string]$Value
    )

    $envPath = Join-Path $repositoryRoot ".env"
    $lines = if (Test-Path -LiteralPath $envPath) {
        @(Get-Content -LiteralPath $envPath)
    }
    else {
        @()
    }
    $replacement = "$Name=$Value"
    $updated = $false
    $result = foreach ($line in $lines) {
        if ($line -match "^$([regex]::Escape($Name))=") {
            if (-not $updated) {
                $replacement
                $updated = $true
            }
        }
        else {
            $line
        }
    }
    if (-not $updated) {
        $result = @($result) + $replacement
    }
    [IO.File]::WriteAllLines(
        $envPath,
        @($result),
        [Text.UTF8Encoding]::new($false)
    )
}

$swirlImage = Get-ConfiguredValue -Name "SWIRL_IMAGE" -DefaultValue "harnes-swirl:4.5.0.7"
& $docker image inspect $swirlImage *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Local SWIRL image '$swirlImage' is not available. Load or build it first."
}

$username = Get-ConfiguredValue -Name "SWIRL_USERNAME" -DefaultValue "admin"
$password = Get-ConfiguredValue -Name "SWIRL_PASSWORD" -DefaultValue "swirl-dev-password"

if (-not $SkipStart) {
    Set-DotEnvValue -Name "SWIRL_IMAGE" -Value $swirlImage
    Set-DotEnvValue -Name "SWIRL_BASE_URL" -Value "http://swirl:8000"
    Set-DotEnvValue -Name "SWIRL_USERNAME" -Value $username
    Set-DotEnvValue -Name "SWIRL_PASSWORD" -Value $password
    Set-DotEnvValue -Name "SWIRL_SANDBOX_NETWORK" -Value "automation-agent-search"

    # Process-scoped values also apply to this Compose invocation immediately.
    $env:SWIRL_IMAGE = $swirlImage
    $env:SWIRL_BASE_URL = "http://swirl:8000"
    $env:SWIRL_USERNAME = $username
    $env:SWIRL_PASSWORD = $password
    $env:SWIRL_SANDBOX_NETWORK = "automation-agent-search"

    & $docker compose --profile lite stop swirl-lite | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to stop the lightweight search service."
    }
    & $docker compose --profile swirl up -d swirl orchestrator worker
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to start SWIRL."
    }
}

$containerId = (& $docker compose ps -q swirl).Trim()
if (-not $containerId) {
    throw "SWIRL container is not running."
}

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
do {
    $health = (& $docker inspect $containerId --format "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}").Trim()
    if ($health -eq "healthy") {
        break
    }
    if ($health -in @("unhealthy", "exited", "dead")) {
        & $docker compose logs --no-color --tail 120 swirl
        throw "SWIRL entered '$health' state."
    }
    Start-Sleep -Seconds 2
} while ((Get-Date) -lt $deadline)

if ($health -ne "healthy") {
    throw "SWIRL did not become healthy within $TimeoutSeconds seconds."
}

$credentials = [System.Text.Encoding]::UTF8.GetBytes("${username}:${password}")
$headers = @{ Authorization = "Basic " + [Convert]::ToBase64String($credentials) }
$escapedQuery = [Uri]::EscapeDataString($Query)
$response = Invoke-RestMethod `
    -Uri "http://localhost:8083/api/swirl/search/?qs=$escapedQuery&result_count=3" `
    -Headers $headers `
    -TimeoutSec 60

if (-not $response.info.search.id -or $null -eq $response.results) {
    throw "SWIRL returned an unexpected search response."
}

$resultCount = @($response.results).Count
Write-Output "SWIRL is healthy: image=$swirlImage search_id=$($response.info.search.id) results=$resultCount"
Write-Output "UI: http://localhost:8083/galaxy/"
