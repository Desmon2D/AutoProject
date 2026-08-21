param(
    [switch]$WithGitea,
    [switch]$CoreOnly
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$envFile = Join-Path $root ".env"
$docker = "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin\docker.exe"
if (-not (Test-Path -LiteralPath $docker)) {
    throw "Docker CLI not found: $docker"
}

function Set-DotEnvValue([string]$Name, [string]$Value) {
    $lines = @()
    if (Test-Path -LiteralPath $envFile) {
        $prefix = "$Name="
        $lines = @([IO.File]::ReadAllLines($envFile) | Where-Object {
            -not $_.StartsWith($prefix, [StringComparison]::Ordinal)
        })
    }
    $lines += "$Name=$Value"
    [IO.File]::WriteAllLines($envFile, $lines, [Text.UTF8Encoding]::new($false))
}

Set-Location $root
& $docker compose --profile swirl stop swirl swirl-redis | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Cannot stop full SWIRL" }
& $docker compose --profile plane stop plane plane-minio plane-rabbitmq plane-redis plane-db | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Cannot stop Plane services" }

if ($CoreOnly) {
    & $docker compose --profile lite stop swirl-lite | Out-Null
    Set-DotEnvValue "SWIRL_BASE_URL" ""
    Set-DotEnvValue "SWIRL_USERNAME" ""
    Set-DotEnvValue "SWIRL_PASSWORD" ""
    Set-DotEnvValue "SWIRL_SANDBOX_NETWORK" ""
}
else {
    Set-DotEnvValue "SWIRL_BASE_URL" "http://swirl-lite:8000"
    Set-DotEnvValue "SWIRL_USERNAME" "local"
    Set-DotEnvValue "SWIRL_PASSWORD" "local-development"
    Set-DotEnvValue "SWIRL_SANDBOX_NETWORK" "automation-agent-search"
}

if (-not $WithGitea) {
    & $docker compose --profile gitea stop gitea | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Cannot stop Gitea" }
}

& $docker compose build orchestrator dashboard
if ($LASTEXITCODE -ne 0) { throw "Cannot build low-memory services" }

$services = @("orchestrator", "worker", "dashboard")
if (-not $CoreOnly) { $services += "swirl-lite" }
if ($WithGitea) { $services += "gitea" }
& $docker compose --profile lite --profile gitea up -d --force-recreate $services
if ($LASTEXITCODE -ne 0) { throw "Cannot start low-memory services" }

$deadline = (Get-Date).AddSeconds(60)
do {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8080/health" -TimeoutSec 2
        if ($health.status -eq "ok" -and $health.queue.worker_online) { break }
    }
    catch { }
    Start-Sleep -Seconds 1
} while ((Get-Date) -lt $deadline)
if ($health.status -ne "ok" -or -not $health.queue.worker_online) {
    throw "Low-memory stack did not become ready"
}

Write-Output "Low-memory mode is ready."
Write-Output "Dashboard: http://127.0.0.1:4173"
Write-Output "Search: $(if ($CoreOnly) { 'disabled' } else { 'SWIRL-compatible lite service' })"
Write-Output "Gitea: $(if ($WithGitea) { 'enabled' } else { 'stopped' })"
& $docker stats --no-stream --format "table {{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}"
