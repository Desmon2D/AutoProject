param(
    [string]$WorkspaceSlug = "automation",
    [string]$ProjectIdentifier = "PAY",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$envFile = Join-Path $root ".env"

function Get-DotEnvValue([string]$Name) {
    if (-not (Test-Path -LiteralPath $envFile)) { return $null }
    $prefix = "$Name="
    foreach ($line in [IO.File]::ReadAllLines($envFile)) {
        if ($line.StartsWith($prefix, [StringComparison]::Ordinal)) {
            return $line.Substring($prefix.Length)
        }
    }
    return $null
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

function Expand-ResponseItems($Value) {
    foreach ($outer in @($Value)) {
        if ($outer -is [Array]) {
            foreach ($inner in $outer) { Write-Output $inner }
        } else {
            Write-Output $outer
        }
    }
}

$email = Get-DotEnvValue "PLANE_ADMIN_EMAIL"
$password = Get-DotEnvValue "PLANE_ADMIN_PASSWORD"
if ([string]::IsNullOrWhiteSpace($email) -or [string]::IsNullOrWhiteSpace($password)) {
    throw "Plane administrator credentials are missing; run start-dev-plane.ps1 first"
}

$baseUrl = "http://127.0.0.1:8081"
$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$csrfToken = (Invoke-RestMethod `
    -Uri "$baseUrl/auth/get-csrf-token/" `
    -WebSession $session `
    -TimeoutSec 10).csrf_token
$headers = @{"X-CSRFToken" = $csrfToken; "Referer" = "$baseUrl/"}
Invoke-WebRequest `
    -Method Post `
    -Uri "$baseUrl/auth/sign-in/" `
    -Headers $headers `
    -Body @{email = $email; password = $password; csrfmiddlewaretoken = $csrfToken} `
    -WebSession $session `
    -UseBasicParsing `
    -TimeoutSec 30 | Out-Null
$csrfToken = (Invoke-RestMethod `
    -Uri "$baseUrl/auth/get-csrf-token/" `
    -WebSession $session `
    -TimeoutSec 10).csrf_token
$headers = @{"X-CSRFToken" = $csrfToken; "Referer" = "$baseUrl/"}

$apiToken = Get-DotEnvValue "PLANE_API_TOKEN"
if ([string]::IsNullOrWhiteSpace($apiToken)) {
    $token = Invoke-RestMethod `
        -Method Post `
        -Uri "$baseUrl/api/users/api-tokens/" `
        -Headers $headers `
        -ContentType "application/json" `
        -Body (@{label = "AutoProject orchestrator"} | ConvertTo-Json -Compress) `
        -WebSession $session `
        -TimeoutSec 30
    $apiToken = $token.token
    if ([string]::IsNullOrWhiteSpace($apiToken)) { throw "Plane did not return an API token" }
    Set-DotEnvValue "PLANE_API_TOKEN" $apiToken
}

$projects = @(Expand-ResponseItems (Invoke-RestMethod `
    -Uri "$baseUrl/api/workspaces/$WorkspaceSlug/projects/" `
    -WebSession $session `
    -TimeoutSec 20))
$project = $projects | Where-Object { $_.identifier -eq $ProjectIdentifier } | Select-Object -First 1
if ($null -eq $project) { throw "Plane project $ProjectIdentifier was not found" }
$stateUrl = "$baseUrl/api/workspaces/$WorkspaceSlug/projects/$($project.id)/states/"
$states = @(Expand-ResponseItems (Invoke-RestMethod -Uri $stateUrl -WebSession $session -TimeoutSec 20))
$ready = $states | Where-Object { $_.name -eq "Ready for development" } | Select-Object -First 1
$testing = $states | Where-Object { $_.name -eq "Testing" } | Select-Object -First 1
$completed = $states | Where-Object { $_.group -eq "completed" } | Select-Object -First 1
$cancelled = $states | Where-Object { $_.group -eq "cancelled" } | Select-Object -First 1
if ($null -eq $ready -or $null -eq $testing -or $null -eq $completed -or $null -eq $cancelled) {
    throw "Plane project must have ready, testing, completed, and cancelled states"
}

Set-DotEnvValue "PLANE_BASE_URL" "http://plane"
Set-DotEnvValue "GITEA_PUBLIC_BASE_URL" "http://localhost:3000"
Set-DotEnvValue "PLANE_WORKSPACE_SLUG" $WorkspaceSlug
Set-DotEnvValue "PLANE_READY_STATE_IDS" $ready.id
Set-DotEnvValue "PLANE_TESTING_STATE_IDS" $testing.id
Set-DotEnvValue "PLANE_COMPLETED_STATE_IDS" $completed.id
Set-DotEnvValue "PLANE_CANCELLED_STATE_IDS" $cancelled.id

Set-Location $root
if (-not $SkipBuild) {
    docker compose build --pull=false orchestrator
    if ($LASTEXITCODE -ne 0) { throw "Cannot build orchestrator" }
}
docker compose up -d --force-recreate orchestrator worker
if ($LASTEXITCODE -ne 0) { throw "Cannot apply Plane synchronization settings" }

Write-Output "Plane result synchronization is configured."
