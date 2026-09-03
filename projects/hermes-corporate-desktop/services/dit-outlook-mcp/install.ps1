[CmdletBinding()]
param(
    [string]$Python = '3.12'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$serviceRoot = $PSScriptRoot
$venvPath = Join-Path $serviceRoot '.venv'
$pythonPath = Join-Path $venvPath 'Scripts\python.exe'

$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
$uvPath = if ($null -ne $uvCommand) {
    $uvCommand.Source
} else {
    Join-Path $env:LOCALAPPDATA 'hermes\bin\uv.exe'
}

if (-not (Test-Path -LiteralPath $uvPath)) {
    throw 'uv was not found. Install uv or add uv to PATH.'
}

if (-not (Test-Path -LiteralPath $pythonPath)) {
    & $uvPath venv $venvPath --python $Python
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to create the Outlook MCP virtual environment.'
    }
}

& $uvPath pip install --python $pythonPath --editable $serviceRoot
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to install the Outlook MCP.'
}

Write-Host "DIT Outlook MCP installed: $venvPath"
Write-Host 'Run .\services\dit-outlook-mcp\.venv\Scripts\exchange-ews-mcp.exe configure to authorize.'
