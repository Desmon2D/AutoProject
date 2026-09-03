[CmdletBinding()]
param(
    [int]$McpoPort = 8000,
    [int]$LiteLlmPort = 4000,
    [string]$ProxyUrl = 'http://127.0.0.1:10808',
    [string]$DitAgentHome = "$env:LOCALAPPDATA\hermes-corporate-dev\agent-home"
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$runtimeDir = Join-Path $PSScriptRoot '.runtime'
$mcpoEnvPath = Join-Path $PSScriptRoot '.env.mcpo'
$openWebUiEnvPath = Join-Path $PSScriptRoot '.env.openwebui'
$composePath = Join-Path $PSScriptRoot 'compose.ai.yaml'
$ditSecretsPath = Join-Path $DitAgentHome '.env'
$mcpoBridgePath = Join-Path $PSScriptRoot 'mcpo_bridge.py'
$mcpProcessPath = Join-Path $PSScriptRoot 'mcp_process.py'
$mcpManifestPath = Join-Path $PSScriptRoot 'mcp-servers.json'
$liteLlmStartPath = Join-Path $PSScriptRoot 'start-litellm.ps1'
$pidPath = Join-Path $runtimeDir 'mcpo.pid'
$runtimeVersionPath = Join-Path $runtimeDir 'mcpo.version'
$stdoutPath = Join-Path $runtimeDir 'mcpo.stdout.log'
$stderrPath = Join-Path $runtimeDir 'mcpo.stderr.log'

function Get-DotEnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }

    $prefix = "$Name="
    $line = Get-Content -LiteralPath $Path |
        Where-Object { $_.StartsWith($prefix, [StringComparison]::Ordinal) } |
        Select-Object -Last 1

    if ($null -eq $line) {
        return $null
    }

    return $line.Substring($prefix.Length).Trim()
}

function Set-DotEnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Value
    )

    $lines = [Collections.Generic.List[string]]::new()
    if (Test-Path -LiteralPath $Path) {
        foreach ($existingLine in (Get-Content -LiteralPath $Path)) {
            $lines.Add($existingLine)
        }
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

    if (-not $replaced) {
        $lines.Add($replacement)
    }

    $text = ($lines -join [Environment]::NewLine) + [Environment]::NewLine
    [IO.File]::WriteAllText($Path, $text, [Text.UTF8Encoding]::new($false))
}

function Resolve-McpExecutable {
    param([Parameter(Mandatory = $true)]$Command)

    $root = switch ($Command.root) {
        'repository' { $repoRoot }
        'localAppData' { $env:LOCALAPPDATA }
        default { throw "Unsupported MCP command root: $($Command.root)" }
    }
    return [IO.Path]::GetFullPath((Join-Path $root $Command.path))
}

function Test-Mcpo {
    param([string]$ApiKey)

    try {
        $headers = @{ Authorization = "Bearer $ApiKey" }
        foreach ($serverName in $mcpServerNames) {
            $spec = Invoke-RestMethod -Uri "http://127.0.0.1:$McpoPort/$serverName/openapi.json" -Headers $headers -TimeoutSec 5
            if ($null -eq $spec.paths -or @($spec.paths.PSObject.Properties).Count -eq 0) {
                return $false
            }
        }
        return $true
    } catch {
        return $false
    }
}

function Stop-ProcessTree {
    param([Parameter(Mandatory = $true)][int]$RootProcessId)

    $allProcesses = @(Get-CimInstance Win32_Process)
    $pending = [Collections.Generic.Queue[int]]::new()
    $processIds = [Collections.Generic.List[int]]::new()
    $pending.Enqueue($RootProcessId)
    while ($pending.Count -gt 0) {
        $currentId = $pending.Dequeue()
        $processIds.Add($currentId)
        foreach ($child in $allProcesses | Where-Object { $_.ParentProcessId -eq $currentId }) {
            $pending.Enqueue([int]$child.ProcessId)
        }
    }
    for ($index = $processIds.Count - 1; $index -ge 0; $index--) {
        Stop-Process -Id $processIds[$index] -Force -ErrorAction SilentlyContinue
    }
}

function Get-ProcessTreeIds {
    param([Parameter(Mandatory = $true)][int]$RootProcessId)

    $allProcesses = @(Get-CimInstance Win32_Process)
    $pending = [Collections.Generic.Queue[int]]::new()
    $processIds = [Collections.Generic.List[int]]::new()
    $pending.Enqueue($RootProcessId)
    while ($pending.Count -gt 0) {
        $currentId = $pending.Dequeue()
        $processIds.Add($currentId)
        foreach ($child in $allProcesses | Where-Object { $_.ParentProcessId -eq $currentId }) {
            $pending.Enqueue([int]$child.ProcessId)
        }
    }
    return $processIds.ToArray()
}

function Wait-OpenWebUiHealthy {
    param([int]$TimeoutSeconds = 90)

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $health = ''
    do {
        Start-Sleep -Seconds 2
        $health = & docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' hermes-corporate-openwebui 2>$null
    } until ($health -eq 'healthy' -or [DateTime]::UtcNow -ge $deadline)
    return $health
}

if (-not (Test-Path -LiteralPath $ditSecretsPath)) {
    throw "DIT Agent secret file was not found: $ditSecretsPath"
}

if (-not (Test-Path -LiteralPath $mcpManifestPath)) {
    throw "MCP server manifest was not found: $mcpManifestPath"
}

$mcpManifest = Get-Content -LiteralPath $mcpManifestPath -Raw -Encoding utf8 | ConvertFrom-Json
$enabledMcpServers = [Collections.Generic.List[object]]::new()
foreach ($server in $mcpManifest.servers) {
    $executable = Resolve-McpExecutable -Command $server.command
    if (-not (Test-Path -LiteralPath $executable)) {
        if ($server.optional) {
            Write-Warning "Skipping optional MCP '$($server.id)': executable was not found at $executable"
            continue
        }
        throw "MCP executable was not found: $executable"
    }

    $missingSecrets = @(
        foreach ($secretName in $server.requiredEnv) {
            $secretValue = Get-DotEnvValue -Path $ditSecretsPath -Name $secretName
            if ([string]::IsNullOrWhiteSpace($secretValue)) {
                $secretName
            }
        }
    )
    if ($missingSecrets.Count -gt 0) {
        Write-Warning "Skipping MCP '$($server.id)': missing $($missingSecrets -join ', ') in $ditSecretsPath"
        continue
    }

    $enabledMcpServers.Add($server)
}
if ($enabledMcpServers.Count -eq 0) {
    throw 'No configured MCP servers are available.'
}
$mcpServerNames = @($enabledMcpServers | ForEach-Object { $_.id })

if (-not (Test-Path -LiteralPath $openWebUiEnvPath)) {
    throw "OpenWebUI environment file was not found: $openWebUiEnvPath"
}

& $liteLlmStartPath -Port $LiteLlmPort -DitAgentHome $DitAgentHome -ProxyUrl $ProxyUrl -ConfigureHermes
$liteLlmKey = Get-DotEnvValue -Path $ditSecretsPath -Name 'LITELLM_MASTER_KEY'
if ([string]::IsNullOrWhiteSpace($liteLlmKey)) {
    throw "LITELLM_MASTER_KEY is missing from $ditSecretsPath"
}

New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null

$manifestHash = (Get-FileHash -LiteralPath $mcpManifestPath -Algorithm SHA256).Hash
$bridgeHash = (Get-FileHash -LiteralPath $mcpoBridgePath -Algorithm SHA256).Hash
$processHash = (Get-FileHash -LiteralPath $mcpProcessPath -Algorithm SHA256).Hash
$desiredRuntimeVersion = "$manifestHash`:$bridgeHash`:$processHash"
$currentRuntimeVersion = if (Test-Path -LiteralPath $runtimeVersionPath) {
    (Get-Content -LiteralPath $runtimeVersionPath -Raw).Trim()
} else {
    ''
}

$mcpoKey = Get-DotEnvValue -Path $mcpoEnvPath -Name 'MCPO_API_KEY'
if ([string]::IsNullOrWhiteSpace($mcpoKey) -or $mcpoKey -eq '<GENERATE_64_HEX_CHARACTERS>') {
    $keyBytes = New-Object byte[] 32
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $random.GetBytes($keyBytes)
    } finally {
        $random.Dispose()
    }
    $mcpoKey = -join ($keyBytes | ForEach-Object { $_.ToString('x2') })
    Set-DotEnvValue -Path $mcpoEnvPath -Name 'MCPO_API_KEY' -Value $mcpoKey
}

$connectionObjects = @(
    foreach ($metadata in $enabledMcpServers) {
        [ordered]@{
            type = 'openapi'
            url = "http://host.docker.internal:$McpoPort/$($metadata.id)"
            path = 'openapi.json'
            auth_type = 'bearer'
            key = $mcpoKey
            config = [ordered]@{ enable = $true }
            info = [ordered]@{
                id = $metadata.id
                name = $metadata.name
                description = $metadata.description
            }
        }
    }
)
$connection = ConvertTo-Json -InputObject $connectionObjects -Depth 6 -Compress
Set-DotEnvValue -Path $openWebUiEnvPath -Name 'TOOL_SERVER_CONNECTIONS' -Value $connection
Set-DotEnvValue -Path $openWebUiEnvPath -Name 'OPENAI_API_BASE_URL' -Value "http://litellm:$LiteLlmPort/v1"
Set-DotEnvValue -Path $openWebUiEnvPath -Name 'OPENAI_API_KEY' -Value $liteLlmKey

$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
$uvPath = if ($null -ne $uvCommand) {
    $uvCommand.Source
} else {
    Join-Path $env:LOCALAPPDATA 'hermes\bin\uv.exe'
}

if (-not (Test-Path -LiteralPath $uvPath)) {
    throw 'uv was not found. Install uv or add uv to PATH.'
}

$mcpoRunning = $currentRuntimeVersion -eq $desiredRuntimeVersion -and (Test-Mcpo -ApiKey $mcpoKey)
if (-not $mcpoRunning) {
    $listener = Get-NetTCPConnection -LocalPort $McpoPort -State Listen -ErrorAction SilentlyContinue
    if ($null -ne $listener) {
        $managedPid = 0
        $managedPidText = if (Test-Path -LiteralPath $pidPath) { (Get-Content -LiteralPath $pidPath -Raw).Trim() } else { '' }
        $managedTree = if ([int]::TryParse($managedPidText, [ref]$managedPid)) {
            @(Get-ProcessTreeIds -RootProcessId $managedPid)
        } else { @() }
        $listenerOwners = @($listener | ForEach-Object { [int]$_.OwningProcess })
        $ownsListener = @($managedTree | Where-Object { $listenerOwners -contains $_ }).Count -gt 0
        if ($ownsListener) {
            Stop-ProcessTree -RootProcessId $managedPid
            Start-Sleep -Milliseconds 500
        } else {
            throw "Port $McpoPort is already occupied by another process."
        }
    }

    $arguments = @(
        'run',
        '--no-project',
        '--with', 'mcpo==0.0.20',
        '--with', 'mcp<2',
        'python',
        $mcpoBridgePath
    )

    $startParameters = @{
        FilePath = $uvPath
        ArgumentList = $arguments
        WorkingDirectory = $repoRoot
        WindowStyle = 'Hidden'
        RedirectStandardOutput = $stdoutPath
        RedirectStandardError = $stderrPath
        PassThru = $true
    }
    $previousMcpoKey = [Environment]::GetEnvironmentVariable('MCPO_API_KEY', 'Process')
    $previousDitAgentHome = [Environment]::GetEnvironmentVariable('DIT_AGENT_HOME', 'Process')
    $previousMcpoPort = [Environment]::GetEnvironmentVariable('DIT_MCPO_PORT', 'Process')
    try {
        $env:MCPO_API_KEY = $mcpoKey
        $env:DIT_AGENT_HOME = $DitAgentHome
        $env:DIT_MCPO_PORT = $McpoPort.ToString()
        $process = Start-Process @startParameters
    } finally {
        [Environment]::SetEnvironmentVariable('MCPO_API_KEY', $previousMcpoKey, 'Process')
        [Environment]::SetEnvironmentVariable('DIT_AGENT_HOME', $previousDitAgentHome, 'Process')
        [Environment]::SetEnvironmentVariable('DIT_MCPO_PORT', $previousMcpoPort, 'Process')
    }

    [IO.File]::WriteAllText($pidPath, $process.Id.ToString(), [Text.UTF8Encoding]::new($false))

    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    do {
        Start-Sleep -Milliseconds 500
        if ($process.HasExited) {
            $tail = if (Test-Path -LiteralPath $stderrPath) {
                (Get-Content -LiteralPath $stderrPath -Tail 20) -join [Environment]::NewLine
            } else {
                'No MCPO error log was created.'
            }
            throw "MCPO stopped during startup.`n$tail"
        }
        $mcpoRunning = Test-Mcpo -ApiKey $mcpoKey
    } until ($mcpoRunning -or [DateTime]::UtcNow -ge $deadline)

    if (-not $mcpoRunning) {
        throw "MCPO did not become ready within 45 seconds. See $stderrPath"
    }
    [IO.File]::WriteAllText($runtimeVersionPath, $desiredRuntimeVersion, [Text.UTF8Encoding]::new($false))
}

& docker compose -f $composePath up -d openwebui
if ($LASTEXITCODE -ne 0) {
    throw 'Docker Compose failed to start OpenWebUI.'
}

$health = Wait-OpenWebUiHealthy
if ($health -ne 'healthy') {
    throw "OpenWebUI did not become healthy within 90 seconds (status: $health)."
}

# OpenWebUI persists this setting in SQLite after the first run, so changing the
# environment variable alone does not replace an older one-server configuration.
# Synchronize only this config key; users, chats, models, and all other settings
# remain untouched. Restart only when the persisted value actually changed.
$syncScript = @'
import asyncio
import json
import os

import open_webui.config
from open_webui.models.config import Config


async def sync() -> None:
    desired = json.loads(os.environ['TOOL_SERVER_CONNECTIONS'])
    current = await Config.get('tool_server.connections', [])
    if current == desired:
        print('unchanged')
        return
    await Config.upsert({'tool_server.connections': desired})
    print('changed')


asyncio.run(sync())
'@
$syncResult = @(& docker exec hermes-corporate-openwebui python -c $syncScript)
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to synchronize OpenWebUI tool server connections.'
}
if (($syncResult | Select-Object -Last 1) -eq 'changed') {
    & docker restart hermes-corporate-openwebui | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to restart OpenWebUI after updating tool server connections.'
    }
    $health = Wait-OpenWebUiHealthy
    if ($health -ne 'healthy') {
        throw "OpenWebUI did not become healthy after tool synchronization (status: $health)."
    }
}

Write-Host "DIT MCP gateways: ready ($($mcpServerNames -join ', '))"
Write-Host "LiteLLM:        http://127.0.0.1:$LiteLlmPort/v1 -> OpenRouter"
Write-Host 'OpenWebUI:      http://localhost:3000'
Write-Host "MCPO schemas:   http://localhost:$McpoPort/<server>/openapi.json (Bearer authentication required)"
