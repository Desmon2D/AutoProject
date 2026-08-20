param(
    [string]$BaseImage = "16788311e2fa",
    [string]$Image = "automation-dsh-sandbox-code:0.1.0-rc.7",
    [string]$DshVersion = "0.1.0-rc.7"
)

$ErrorActionPreference = "Stop"
$docker = "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin\docker.exe"
if (-not (Test-Path -LiteralPath $docker)) {
    throw "Docker CLI not found: $docker"
}

$builder = "automation-dsh-deps-builder"
try {
    & $docker rm --force $builder 2>$null | Out-Null
    & $docker run --detach --name $builder --dns 1.1.1.1 --entrypoint sh $BaseImage -lc "sleep infinity"
    if ($LASTEXITCODE -ne 0) { throw "Cannot start dependency builder" }

    & $docker exec $builder npm install --global `
        --allow-scripts="@deepseek-ai/dsh-subprocess-local,koffi,node-pty,@google/genai,protobufjs" `
        "@deepseek-ai/dsh@$DshVersion"
    if ($LASTEXITCODE -ne 0) { throw "Cannot install @deepseek-ai/dsh@$DshVersion" }

    & $docker exec $builder dsh --help
    if ($LASTEXITCODE -ne 0) { throw "Installed dsh failed its CLI smoke test" }

    & $docker commit $builder "local/dsh-deps:$DshVersion" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Cannot commit dependency image" }
}
finally {
    & $docker rm --force $builder 2>$null | Out-Null
}

& $docker build --network none --file (Join-Path $PSScriptRoot "..\Dockerfile.offline") --tag $Image (Join-Path $PSScriptRoot "..")
if ($LASTEXITCODE -ne 0) {
    throw "Offline Docker build failed with exit code $LASTEXITCODE"
}
