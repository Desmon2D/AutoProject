[CmdletBinding()]
param(
    [int]$Port = 4000,
    [string]$Model = 'deepseek/deepseek-v4-flash',
    [string]$ProxyUrl = 'http://127.0.0.1:10808',
    [string]$DitAgentHome = "$env:LOCALAPPDATA\hermes-corporate-dev\agent-home",
    [switch]$ConfigureHermes
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$composePath = Join-Path $PSScriptRoot 'compose.ai.yaml'
$configPath = Join-Path $PSScriptRoot 'litellm.config.yaml'
$environmentPath = Join-Path $DitAgentHome 'litellm.docker.env'
$configurePath = Join-Path $PSScriptRoot 'configure_litellm.py'
$secretsPath = Join-Path $DitAgentHome '.env'
$agentConfigPath = Join-Path $DitAgentHome 'config.yaml'

function Get-DotEnvValue {
    param([string]$Path, [string]$Name)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $prefix = "$Name="
    $line = Get-Content -LiteralPath $Path |
        Where-Object { $_.StartsWith($prefix, [StringComparison]::Ordinal) } |
        Select-Object -Last 1
    if ($null -eq $line) { return $null }
    return $line.Substring($prefix.Length).Trim()
}

function Set-DotEnvValue {
    param([string]$Path, [string]$Name, [string]$Value)
    $lines = [Collections.Generic.List[string]]::new()
    if (Test-Path -LiteralPath $Path) {
        foreach ($line in Get-Content -LiteralPath $Path) { $lines.Add($line) }
    }
    $prefix = "$Name="
    $replacement = "$prefix$Value"
    $replaced = $false
    for ($index = 0; $index -lt $lines.Count; $index++) {
        if ($lines[$index].StartsWith($prefix, [StringComparison]::Ordinal)) {
            $lines[$index] = $replacement
            $replaced = $true
        }
    }
    if (-not $replaced) { $lines.Add($replacement) }
    [IO.File]::WriteAllText(
        $Path,
        ($lines -join [Environment]::NewLine) + [Environment]::NewLine,
        [Text.UTF8Encoding]::new($false)
    )
}

function Test-LiteLlm {
    param([string]$ApiKey)
    try {
        $headers = @{ Authorization = "Bearer $ApiKey" }
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health/liveliness" -Headers $headers -TimeoutSec 5
        return $null -ne $response
    } catch {
        return $false
    }
}

if ($Port -ne 4000) {
    throw 'compose.ai.yaml currently publishes LiteLLM on port 4000. Use -Port 4000.'
}
if (-not (Test-Path -LiteralPath $secretsPath)) {
    throw "DIT Agent secret file was not found: $secretsPath"
}
if (-not (Test-Path -LiteralPath $configPath)) {
    throw "LiteLLM config was not found: $configPath"
}

$openRouterKey = Get-DotEnvValue -Path $secretsPath -Name 'OPENROUTER_API_KEY'
if ([string]::IsNullOrWhiteSpace($openRouterKey)) {
    throw "OPENROUTER_API_KEY is missing from $secretsPath"
}

$masterKey = Get-DotEnvValue -Path $secretsPath -Name 'LITELLM_MASTER_KEY'
if ([string]::IsNullOrWhiteSpace($masterKey)) {
    $keyBytes = New-Object byte[] 32
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $random.GetBytes($keyBytes) } finally { $random.Dispose() }
    $masterKey = 'sk-local-' + (-join ($keyBytes | ForEach-Object { $_.ToString('x2') }))
    Set-DotEnvValue -Path $secretsPath -Name 'LITELLM_MASTER_KEY' -Value $masterKey
}

$uiUsername = Get-DotEnvValue -Path $secretsPath -Name 'UI_USERNAME'
if ([string]::IsNullOrWhiteSpace($uiUsername)) {
    $uiUsername = 'admin'
    Set-DotEnvValue -Path $secretsPath -Name 'UI_USERNAME' -Value $uiUsername
}

$uiPassword = Get-DotEnvValue -Path $secretsPath -Name 'UI_PASSWORD'
if ([string]::IsNullOrWhiteSpace($uiPassword)) {
    $passwordBytes = New-Object byte[] 24
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $random.GetBytes($passwordBytes) } finally { $random.Dispose() }
    $uiPassword = 'ui-' + (-join ($passwordBytes | ForEach-Object { $_.ToString('x2') }))
    Set-DotEnvValue -Path $secretsPath -Name 'UI_PASSWORD' -Value $uiPassword
}

$postgresPassword = Get-DotEnvValue -Path $secretsPath -Name 'LITELLM_POSTGRES_PASSWORD'
if ([string]::IsNullOrWhiteSpace($postgresPassword)) {
    $passwordBytes = New-Object byte[] 24
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $random.GetBytes($passwordBytes) } finally { $random.Dispose() }
    $postgresPassword = 'pg-' + (-join ($passwordBytes | ForEach-Object { $_.ToString('x2') }))
    Set-DotEnvValue -Path $secretsPath -Name 'LITELLM_POSTGRES_PASSWORD' -Value $postgresPassword
}

$containerProxyUrl = $ProxyUrl -replace '://(localhost|127\.0\.0\.1)(?=[:/])', '://host.docker.internal'
$environmentLines = @(
    "OPENROUTER_API_KEY=$openRouterKey"
    "LITELLM_MASTER_KEY=$masterKey"
    "UI_USERNAME=$uiUsername"
    "UI_PASSWORD=$uiPassword"
    'POSTGRES_DB=litellm'
    'POSTGRES_USER=litellm'
    "POSTGRES_PASSWORD=$postgresPassword"
    "DATABASE_URL=postgresql://litellm:$postgresPassword@litellm-postgres:5432/litellm"
    "HTTP_PROXY=$containerProxyUrl"
    "HTTPS_PROXY=$containerProxyUrl"
    'NO_PROXY=localhost,127.0.0.1,::1'
)
[IO.File]::WriteAllText(
    $environmentPath,
    ($environmentLines -join [Environment]::NewLine) + [Environment]::NewLine,
    [Text.UTF8Encoding]::new($false)
)
$env:DIT_LITELLM_ENV_FILE = $environmentPath

& docker version --format '{{.Server.Version}}' *> $null
if ($LASTEXITCODE -ne 0) {
    throw 'Docker Desktop is not running.'
}

& docker compose -f $composePath up -d litellm
if ($LASTEXITCODE -ne 0) {
    throw 'Docker Compose failed to start LiteLLM.'
}

$deadline = [DateTime]::UtcNow.AddSeconds(180)
$running = $false
do {
    Start-Sleep -Seconds 2
    $running = Test-LiteLlm -ApiKey $masterKey
} until ($running -or [DateTime]::UtcNow -ge $deadline)
if (-not $running) {
    $logs = (& docker compose -f $composePath logs --tail 40 litellm 2>&1) -join [Environment]::NewLine
    throw "LiteLLM did not become ready within 180 seconds.`n$logs"
}

if ($ConfigureHermes) {
    if (-not (Test-Path -LiteralPath $agentConfigPath)) {
        throw "DIT Agent config was not found: $agentConfigPath"
    }
    $pythonPath = Join-Path $repoRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        throw "Project Python was not found: $pythonPath"
    }
    & $pythonPath $configurePath --config $agentConfigPath --model $Model --base-url "http://127.0.0.1:$Port/v1"
    if ($LASTEXITCODE -ne 0) { throw 'Failed to configure DIT Agent for LiteLLM.' }
}

Write-Host "LiteLLM container: ready at http://127.0.0.1:$Port/v1"
Write-Host "Model:             $Model -> OpenRouter"
Write-Host "Container proxy:   $containerProxyUrl"
Write-Host "Admin UI:          http://127.0.0.1:$Port/ui/"
Write-Host "UI username:       $uiUsername"
Write-Host "UI password:       stored as UI_PASSWORD in $secretsPath"
