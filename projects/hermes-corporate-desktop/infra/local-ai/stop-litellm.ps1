[CmdletBinding()]
param(
    [string]$DitAgentHome = "$env:LOCALAPPDATA\hermes-corporate-dev\agent-home"
)

$ErrorActionPreference = 'Stop'
$composePath = Join-Path $PSScriptRoot 'compose.ai.yaml'
$environmentPath = Join-Path $DitAgentHome 'litellm.docker.env'
if (-not (Test-Path -LiteralPath $environmentPath)) {
    throw "LiteLLM Docker environment file was not found: $environmentPath"
}
$env:DIT_LITELLM_ENV_FILE = $environmentPath

& docker compose -f $composePath stop litellm
if ($LASTEXITCODE -ne 0) {
    throw 'Docker Compose failed to stop LiteLLM.'
}

Write-Host 'LiteLLM container stopped.'
