param(
    [string]$DshVersion = "0.1.0-rc.7"
)

$ErrorActionPreference = "Stop"
$buildScript = Join-Path $PSScriptRoot "build.ps1"

& $buildScript -Profile core -DshVersion $DshVersion
if ($LASTEXITCODE -ne 0) { throw "Core sandbox build failed" }

& $buildScript -Profile code -DshVersion $DshVersion
if ($LASTEXITCODE -ne 0) { throw "Code sandbox build failed" }

& $buildScript -Profile delivery -DshVersion $DshVersion
if ($LASTEXITCODE -ne 0) { throw "Delivery sandbox build failed" }
