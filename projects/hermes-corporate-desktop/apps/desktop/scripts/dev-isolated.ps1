param(
    [string]$ProxyUrl = 'http://127.0.0.1:10808',
    [string]$ProxyBypass = 'localhost,127.0.0.1,::1,.mos.ru',
    [switch]$SkipLiteLlm
)

$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$isolationRoot = Join-Path $env:LOCALAPPDATA 'hermes-corporate-dev'

$env:HERMES_HOME = Join-Path $isolationRoot 'agent-home'
$env:HERMES_DESKTOP_USER_DATA_DIR = Join-Path $isolationRoot 'desktop-user-data'
$env:HERMES_DESKTOP_HERMES_ROOT = $repoRoot
$env:HERMES_DESKTOP_APP_NAME = 'DIT Agent Dev'
$env:HERMES_DESKTOP_CDP_PORT = '9223'

if (-not $SkipLiteLlm) {
    $liteLlmStartPath = Join-Path $repoRoot 'infra\local-ai\start-litellm.ps1'
    & $liteLlmStartPath -DitAgentHome $env:HERMES_HOME -ProxyUrl $ProxyUrl -ConfigureHermes
}

# The Python backend does not use the Windows system-proxy setting. Pass the
# local v2rayN mixed HTTP proxy explicitly while keeping corporate and local
# services on a direct route. Child MCP servers inherit the same bypass list.
$env:HTTP_PROXY = $ProxyUrl
$env:HTTPS_PROXY = $ProxyUrl
$env:http_proxy = $ProxyUrl
$env:https_proxy = $ProxyUrl
$env:NO_PROXY = $ProxyBypass
$env:no_proxy = $ProxyBypass

Write-Host "Isolated HERMES_HOME: $env:HERMES_HOME"
Write-Host "Isolated Electron data: $env:HERMES_DESKTOP_USER_DATA_DIR"
Write-Host "Provider proxy: $ProxyUrl"
Write-Host "Proxy bypass: $ProxyBypass"

npm run dev
exit $LASTEXITCODE
