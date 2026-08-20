param(
    [ValidateSet("core", "code", "delivery")]
    [string]$Profile = "code",
    [string]$Image = "",
    [string]$DshVersion = "0.1.0-rc.7"
)

$ErrorActionPreference = "Stop"
$docker = "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin\docker.exe"
if (-not (Test-Path -LiteralPath $docker)) {
    throw "Docker CLI not found: $docker"
}
if ([string]::IsNullOrWhiteSpace($Image)) {
    $Image = "automation-dsh-sandbox-$Profile`:$DshVersion"
}

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$dockerfile = Join-Path $repositoryRoot "sandbox\Dockerfile"
& $docker build --file $dockerfile --build-arg "DSH_VERSION=$DshVersion" --target $Profile --tag $Image $repositoryRoot
if ($LASTEXITCODE -ne 0) {
    throw "Docker build failed with exit code $LASTEXITCODE"
}
