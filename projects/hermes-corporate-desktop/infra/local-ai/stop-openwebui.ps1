[CmdletBinding()]
param(
    [string]$DitAgentHome = "$env:LOCALAPPDATA\hermes-corporate-dev\agent-home"
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$composePath = Join-Path $PSScriptRoot 'compose.ai.yaml'
$liteLlmEnvironmentPath = Join-Path $DitAgentHome 'litellm.docker.env'
$env:DIT_LITELLM_ENV_FILE = if (Test-Path -LiteralPath $liteLlmEnvironmentPath) {
    $liteLlmEnvironmentPath
} else {
    Join-Path $PSScriptRoot 'litellm.env.example'
}
$pidPath = Join-Path $PSScriptRoot '.runtime\mcpo.pid'
$runtimeVersionPath = Join-Path $PSScriptRoot '.runtime\mcpo.version'

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

& docker compose -f $composePath stop openwebui
if ($LASTEXITCODE -ne 0) {
    throw 'Docker Compose failed to stop OpenWebUI.'
}

if (Test-Path -LiteralPath $pidPath) {
    $pidText = (Get-Content -LiteralPath $pidPath -Raw).Trim()
    $mcpoPid = 0
    if ([int]::TryParse($pidText, [ref]$mcpoPid)) {
        $process = Get-Process -Id $mcpoPid -ErrorAction SilentlyContinue
        if ($null -ne $process) {
            Stop-ProcessTree -RootProcessId $mcpoPid
        }
    }
    Remove-Item -LiteralPath $pidPath -Force
}

if (Test-Path -LiteralPath $runtimeVersionPath) {
    Remove-Item -LiteralPath $runtimeVersionPath -Force
}

Write-Host 'OpenWebUI and DIT corporate MCPO gateways stopped.'
