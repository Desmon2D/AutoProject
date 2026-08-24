param(
    [string]$Username = "harnes",
    [string]$Repository = "payments-api",
    [string]$Password = $env:GITEA_DEV_PASSWORD
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$smokeScripts = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\smoke")).Path
$compose = Join-Path $root "compose.yaml"
$envFile = Join-Path $root ".env"
$docker = "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin\docker.exe"
if (-not (Test-Path -LiteralPath $docker)) {
    throw "Docker CLI not found: $docker"
}
if ([string]::IsNullOrWhiteSpace($Password)) {
    $Password = "harnes-local-password"
}

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

& $docker compose --project-directory $root --file $compose up -d gitea
if ($LASTEXITCODE -ne 0) { throw "Cannot start the existing Gitea installation" }

$ready = $false
for ($attempt = 1; $attempt -le 60; $attempt++) {
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:3000/api/v1/version" -TimeoutSec 2 | Out-Null
        $ready = $true
        break
    }
    catch {
        Start-Sleep -Seconds 2
    }
}
if (-not $ready) { throw "Gitea did not become healthy" }

$token = Get-DotEnvValue "GITEA_TOKEN"
$tokenIsValid = $false
if (-not [string]::IsNullOrWhiteSpace($token)) {
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:3000/api/v1/user" `
            -Headers @{ Authorization = "token $token" } -TimeoutSec 5 | Out-Null
        $tokenIsValid = $true
    }
    catch { }
}

if (-not $tokenIsValid) {
    $basicBytes = [Text.Encoding]::UTF8.GetBytes("${Username}:$Password")
    $basic = [Convert]::ToBase64String($basicBytes)
    $tokenName = "automation-dev-" + [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $tokenResponse = Invoke-RestMethod `
        -Method Post `
        -Uri "http://127.0.0.1:3000/api/v1/users/$Username/tokens" `
        -Headers @{ Authorization = "Basic $basic" } `
        -ContentType "application/json" `
        -Body (@{
            name = $tokenName
            scopes = @("write:repository", "write:issue", "read:user")
        } | ConvertTo-Json)
    $token = $tokenResponse.sha1
    if ([string]::IsNullOrWhiteSpace($token)) { throw "Gitea did not return an access token" }
    Set-DotEnvValue "GITEA_TOKEN" $token
}
Set-DotEnvValue "GITEA_BASE_URL" "http://gitea:3000"
Set-DotEnvValue "GITEA_USERNAME" $Username
Set-DotEnvValue "GITEA_ALLOWED_REPOSITORIES" "$Username/$Repository"
$webhookSecret = Get-DotEnvValue "GITEA_WEBHOOK_SECRET"
if ([string]::IsNullOrWhiteSpace($webhookSecret)) {
    $secretBytes = New-Object byte[] 32
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $random.GetBytes($secretBytes) } finally { $random.Dispose() }
    $webhookSecret = -join ($secretBytes | ForEach-Object { $_.ToString("x2") })
    Set-DotEnvValue "GITEA_WEBHOOK_SECRET" $webhookSecret
}

$repositoryInfo = Invoke-RestMethod `
    -Uri "http://127.0.0.1:3000/api/v1/repos/$Username/$Repository" `
    -Headers @{ Authorization = "token $token" } `
    -TimeoutSec 5

& $docker compose --project-directory $root --file $compose up -d --force-recreate orchestrator worker
if ($LASTEXITCODE -ne 0) { throw "Cannot restart orchestrator with Gitea credentials" }

$orchestratorReady = $false
$orchestratorHealth = $null
for ($attempt = 1; $attempt -le 30; $attempt++) {
    try {
        $orchestratorHealth = Invoke-RestMethod `
            -Uri "http://127.0.0.1:8080/health" `
            -TimeoutSec 2
        $orchestratorReady = $true
        break
    }
    catch { Start-Sleep -Seconds 1 }
}
if (-not $orchestratorReady) { throw "Orchestrator did not become healthy" }
if (-not $orchestratorHealth.providers.openrouter.configured) {
    throw "OpenRouter is not configured; run scripts/setup/configure-openrouter.ps1 first"
}

$apiHeaders = @{ Authorization = "token $token" }
$webhookUrl = "http://orchestrator:8080/v1/webhooks/gitea"
$hookResponse = Invoke-RestMethod `
    -Uri "http://127.0.0.1:3000/api/v1/repos/$Username/$Repository/hooks" `
    -Headers $apiHeaders `
    -TimeoutSec 5
$hooks = @($hookResponse)
$existingHook = $hooks | Where-Object { $_.config.url -eq $webhookUrl } | Select-Object -First 1
$hookConfig = @{
    active = $true
    branch_filter = "**"
    events = @("pull_request", "pull_request_review")
    config = @{
        url = $webhookUrl
        content_type = "json"
        secret = $webhookSecret
    }
}
if ($null -eq $existingHook) {
    $createHook = @{ type = "gitea" } + $hookConfig
    $configuredHook = Invoke-RestMethod `
        -Method Post `
        -Uri "http://127.0.0.1:3000/api/v1/repos/$Username/$Repository/hooks" `
        -Headers $apiHeaders `
        -ContentType "application/json" `
        -Body ($createHook | ConvertTo-Json -Depth 5)
}
else {
    $configuredHook = Invoke-RestMethod `
        -Method Patch `
        -Uri "http://127.0.0.1:3000/api/v1/repos/$Username/$Repository/hooks/$($existingHook.id)" `
        -Headers $apiHeaders `
        -ContentType "application/json" `
        -Body ($hookConfig | ConvertTo-Json -Depth 5)
}

& $docker run --rm `
    --network automation-agent-source `
    --env "GITEA_TOKEN=$token" `
    --env "GITEA_USERNAME=$Username" `
    --env "GITEA_REPOSITORY=$Repository" `
    --volume "${smokeScripts}:/bootstrap:ro" `
    --entrypoint sh `
    automation-dsh-sandbox-code:0.1.0-rc.7 `
    /bootstrap/check-dev-gitea.sh
if ($LASTEXITCODE -ne 0) { throw "Sandbox cannot reach the Gitea Git source" }

[pscustomobject]@{
    Gitea = "http://127.0.0.1:3000"
    Repository = $repositoryInfo.html_url
    GitSource = "http://gitea:3000/$Username/$Repository.git"
    Username = $Username
    TokenConfigured = $true
    WebhookConfigured = $configuredHook.active
    WebhookEvents = @($configuredHook.events) -join ", "
    GitSourceVerified = $true
} | Format-List
