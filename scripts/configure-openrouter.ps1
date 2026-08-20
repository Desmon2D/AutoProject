param(
    [string]$Model = "openai/gpt-4.1-nano",
    [switch]$SkipSmoke,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$envFile = Join-Path $root ".env"

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

$secureKey = Read-Host "OpenRouter API key" -AsSecureString
$pointer = [IntPtr]::Zero
$plainKey = $null
try {
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    $plainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    if ([string]::IsNullOrWhiteSpace($plainKey)) { throw "OpenRouter API key cannot be empty" }

    $headers = @{ Authorization = "Bearer $plainKey" }
    Invoke-RestMethod `
        -Uri "https://openrouter.ai/api/v1/key" `
        -Headers $headers `
        -TimeoutSec 20 | Out-Null
    $catalog = Invoke-RestMethod `
        -Uri "https://openrouter.ai/api/v1/models" `
        -TimeoutSec 30
    $selected = $catalog.data | Where-Object { $_.id -eq $Model } | Select-Object -First 1
    if ($null -eq $selected) { throw "OpenRouter model is unavailable: $Model" }
    if (-not ($selected.supported_parameters -contains "tools")) {
        throw "Selected model does not advertise tool calling: $Model"
    }

    Set-DotEnvValue "OPENROUTER_API_KEY" $plainKey
    Set-DotEnvValue "DEFAULT_AGENT_PROVIDER" "openrouter"
    Set-DotEnvValue "DEFAULT_AGENT_MODEL" $Model

    [pscustomobject]@{
        Provider = "openrouter"
        Model = $Model
        InputPerMillion = "`$$([decimal]$selected.pricing.prompt * 1000000)"
        OutputPerMillion = "`$$([decimal]$selected.pricing.completion * 1000000)"
        KeyStored = $true
    } | Format-List
}
finally {
    if ($pointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
    $plainKey = $null
}

if (-not $SkipSmoke) {
    & (Join-Path $PSScriptRoot "openrouter-smoke.ps1") `
        -Model $Model `
        -SkipBuild:$SkipBuild
}
