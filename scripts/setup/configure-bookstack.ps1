param(
    [string]$BookStackContainer = "harnes-bookstack-1",
    [string]$BookStackDatabaseContainer = "harnes-bookstack-db-1",
    [string]$Query = "payment retry idempotency"
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$envFile = Join-Path $root ".env"
$docker = "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin\docker.exe"
if (-not (Test-Path -LiteralPath $docker)) { throw "Docker CLI not found: $docker" }

function Get-DotEnvValue([string]$Name, [string]$DefaultValue) {
    if (Test-Path -LiteralPath $envFile) {
        $prefix = "$Name="
        foreach ($line in [IO.File]::ReadAllLines($envFile)) {
            if ($line.StartsWith($prefix, [StringComparison]::Ordinal)) {
                $value = $line.Substring($prefix.Length)
                if (-not [string]::IsNullOrWhiteSpace($value)) { return $value }
            }
        }
    }
    return $DefaultValue
}

function Set-DotEnvValue([string]$Name, [string]$Value) {
    $lines = @()
    if (Test-Path -LiteralPath $envFile) {
        $prefix = "$Name="
        $lines = @([IO.File]::ReadAllLines($envFile) | Where-Object {
            -not $_.StartsWith($prefix, [StringComparison]::Ordinal)
        })
    }
    $lines += "$Name=$Value"
    [IO.File]::WriteAllLines($envFile, $lines, [Text.UTF8Encoding]::new($false))
}

Set-Location $root
& $docker start $BookStackDatabaseContainer $BookStackContainer | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Cannot start BookStack containers" }

$networks = & $docker inspect $BookStackContainer --format "{{json .NetworkSettings.Networks}}"
if ($LASTEXITCODE -ne 0) { throw "Cannot inspect BookStack container" }
if ($networks -notmatch 'autoproject_default') {
    & $docker network connect --alias bookstack autoproject_default $BookStackContainer
    if ($LASTEXITCODE -ne 0) { throw "Cannot connect BookStack to AutoProject network" }
}

$deadline = (Get-Date).AddMinutes(2)
do {
    try {
        $response = Invoke-WebRequest "http://127.0.0.1:8082" -UseBasicParsing -TimeoutSec 3
        if ($response.StatusCode -eq 200) { break }
    }
    catch { }
    Start-Sleep -Seconds 2
} while ((Get-Date) -lt $deadline)
if ($response.StatusCode -ne 200) { throw "BookStack did not become ready" }

$php = @'
require "/app/www/vendor/autoload.php";
$app = require "/app/www/bootstrap/app.php";
$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
BookStack\Api\ApiToken::query()->where("name", "AutoProject SWIRL")->delete();
$secret = Illuminate\Support\Str::random(32);
$tokenId = Illuminate\Support\Str::random(32);
$token = new BookStack\Api\ApiToken();
$token->forceFill([
    "name" => "AutoProject SWIRL",
    "token_id" => $tokenId,
    "secret" => Illuminate\Support\Facades\Hash::make($secret),
    "user_id" => 1,
    "expires_at" => BookStack\Api\ApiToken::defaultExpiry(),
]);
$token->save();
echo json_encode(["token_id" => $tokenId, "token_secret" => $secret]);
'@
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($php))
$launcher = 'eval(base64_decode($argv[1]));'
$tokenJson = & $docker exec $BookStackContainer php -r $launcher $encoded
if ($LASTEXITCODE -ne 0) { throw "Cannot create BookStack API token" }
$token = $tokenJson | ConvertFrom-Json
if ([string]::IsNullOrWhiteSpace($token.token_id) -or
    [string]::IsNullOrWhiteSpace($token.token_secret)) {
    throw "BookStack returned an invalid API token"
}

Set-DotEnvValue "BOOKSTACK_BASE_URL" "http://bookstack"
Set-DotEnvValue "BOOKSTACK_TOKEN_ID" $token.token_id
Set-DotEnvValue "BOOKSTACK_TOKEN_SECRET" $token.token_secret

& $docker compose --profile swirl up -d --force-recreate swirl orchestrator worker
if ($LASTEXITCODE -ne 0) { throw "Cannot restart SWIRL with BookStack credentials" }

$deadline = (Get-Date).AddMinutes(5)
do {
    $containerId = (& $docker compose ps -q swirl).Trim()
    if ($containerId) {
        $health = (& $docker inspect $containerId --format "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}").Trim()
        if ($health -eq "healthy") { break }
    }
    Start-Sleep -Seconds 2
} while ((Get-Date) -lt $deadline)
if ($health -ne "healthy") { throw "SWIRL did not become healthy" }

$swirlUser = Get-DotEnvValue "SWIRL_USERNAME" "admin"
$swirlPassword = Get-DotEnvValue "SWIRL_PASSWORD" "swirl-dev-password"
$credentials = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("${swirlUser}:${swirlPassword}"))
$headers = @{ Authorization = "Basic $credentials" }
$queryString = [Uri]::EscapeDataString($Query)
$search = Invoke-RestMethod `
    -Uri "http://127.0.0.1:8083/api/swirl/search/?qs=$queryString&providers=bookstack&result_count=5" `
    -Headers $headers `
    -TimeoutSec 90
$results = @()
foreach ($item in @($search.results)) {
    if ($item.json_results) { $results += @($item.json_results) }
    else { $results += $item }
}
if (-not $search.info.search.id -or $results.Count -eq 0) {
    throw "SWIRL returned no BookStack results"
}

$firstResult = $results | Where-Object { $_.payload.id } | Select-Object -First 1
if (-not $firstResult) { throw "SWIRL BookStack result has no document id" }
$catalog = Invoke-RestMethod `
    -Uri "http://127.0.0.1:8083/api/swirl/searchproviders/" `
    -Headers $headers `
    -TimeoutSec 30
$provider = $null
foreach ($candidate in $catalog) {
    if ($candidate.name -eq "Local BookStack") {
        $provider = $candidate
        break
    }
}
$route = $provider.page_fetch_config_json.automation_content
if (-not $provider -or -not $route.url_template -or -not $route.content_path) {
    throw "SWIRL BookStack provider has no full-content route"
}
$safeRoutes = ConvertTo-Json -InputObject @(
    [ordered]@{
        source = [string]$provider.name
        provider_id = [int]$provider.id
        url_template = [string]$route.url_template
        content_path = [string]$route.content_path
        format = [string]$route.format
    }
) -Compress
Set-DotEnvValue "SWIRL_CONTENT_ALLOWED_ORIGINS" "http://bookstack"
Set-DotEnvValue "SWIRL_CONTENT_ROUTES_JSON" $safeRoutes
$documentUrl = $route.url_template.Replace(
    "{id}",
    [Uri]::EscapeDataString([string]$firstResult.payload.id)
)
$fetchUrl = "http://127.0.0.1:8083/api/swirl/fetch-document/?url=$([Uri]::EscapeDataString($documentUrl))&provider_id=$($provider.id)"
$document = Invoke-RestMethod -Uri $fetchUrl -Headers $headers -TimeoutSec 30
$content = $document.($route.content_path)
if ($content -isnot [string] -or $content.Length -lt 100) {
    throw "SWIRL returned no substantive BookStack document content"
}

& $docker compose up -d --no-deps --force-recreate orchestrator worker
if ($LASTEXITCODE -ne 0) { throw "Cannot apply the safe SWIRL content route" }

[pscustomobject]@{
    BookStack = "http://127.0.0.1:8082"
    Provider = "Local BookStack"
    SearchId = $search.info.search.id
    Results = $results.Count
    FetchedCharacters = $content.Length
    TokenStored = $true
} | Format-List
