param(
    [string]$JobDirectory = (Join-Path $PSScriptRoot "..\examples\openai-smoke"),
    [string]$Image = "automation-dsh-sandbox-code:0.1.0-rc.7",
    [string]$DnsServer = ""
)

$ErrorActionPreference = "Stop"
$docker = "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin\docker.exe"
if (-not (Test-Path -LiteralPath $docker)) {
    throw "Docker CLI not found: $docker"
}
if ([string]::IsNullOrWhiteSpace($env:OPENAI_API_KEY)) {
    throw "Set OPENAI_API_KEY in this PowerShell session before running the sandbox"
}

$job = (Resolve-Path -LiteralPath $JobDirectory).Path
$input = (Resolve-Path -LiteralPath (Join-Path $job "input")).Path
$workspace = (Resolve-Path -LiteralPath (Join-Path $job "workspace")).Path
$output = (Resolve-Path -LiteralPath (Join-Path $job "output")).Path
$name = "automation-dsh-" + [Guid]::NewGuid().ToString("N").Substring(0, 12)

$arguments = @(
    "run", "--rm",
    "--name", $name,
    "--memory", "2g",
    "--cpus", "2",
    "--pids-limit", "256",
    "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges",
    "--read-only",
    "--tmpfs", "/tmp:rw,nosuid,nodev,size=128m",
    "--tmpfs", "/home/sandbox:rw,nosuid,nodev,uid=10001,gid=10001,mode=0700,size=256m",
    "--mount", "type=bind,source=$input,target=/job/input,readonly",
    "--mount", "type=bind,source=$workspace,target=/workspace",
    "--mount", "type=bind,source=$output,target=/output",
    "--env", "OPENAI_API_KEY",
    $Image
)

if (-not [string]::IsNullOrWhiteSpace($DnsServer)) {
    $imageIndex = $arguments.Count - 1
    $arguments = $arguments[0..($imageIndex - 1)] + @("--dns", $DnsServer) + $arguments[$imageIndex]
}

& $docker @arguments
$exitCode = $LASTEXITCODE
if (Test-Path -LiteralPath (Join-Path $output "result.json")) {
    Get-Content -Raw -LiteralPath (Join-Path $output "result.json")
}
exit $exitCode
