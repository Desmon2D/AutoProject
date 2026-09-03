param(
    [string]$Title = "Document the payment retry smoke scenario",
    [string]$Description = "Create AUTOMATION_SMOKE.md. Add a short section titled Payment retry smoke scenario, explain that this file verifies the local automation path, and state that no application behavior is changed. Do not modify application code. Run the relevant repository checks.",
    [string]$ProjectIdentifier = "PAY",
    [ValidateSet("Ready for development", "Testing")]
    [string]$StateName = "Ready for development",
    [string]$ImplementationRef = "main"
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$envFile = Join-Path $root ".env"

function Get-DotEnvValue([string]$Name) {
    if (-not (Test-Path -LiteralPath $envFile)) { return $null }
    $prefix = "$Name="
    foreach ($line in [IO.File]::ReadAllLines($envFile)) {
        if ($line.StartsWith($prefix, [StringComparison]::Ordinal)) {
            return $line.Substring($prefix.Length)
        }
    }
    return $null
}

function Expand-ResponseItems($Value) {
    foreach ($outer in @($Value)) {
        if ($outer -is [Array]) {
            foreach ($inner in $outer) { Write-Output $inner }
        } else {
            Write-Output $outer
        }
    }
}

$adminEmail = Get-DotEnvValue "PLANE_ADMIN_EMAIL"
$adminPassword = Get-DotEnvValue "PLANE_ADMIN_PASSWORD"
if ([string]::IsNullOrWhiteSpace($adminEmail) -or [string]::IsNullOrWhiteSpace($adminPassword)) {
    throw "Run .\scripts\dev\start-dev-plane.ps1 first"
}

$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$csrfToken = (Invoke-RestMethod `
    -Uri "http://127.0.0.1:8081/auth/get-csrf-token/" `
    -WebSession $session `
    -TimeoutSec 10).csrf_token
$headers = @{
    "X-CSRFToken" = $csrfToken
    "Referer" = "http://127.0.0.1:8081/"
}
Invoke-WebRequest `
    -Method Post `
    -Uri "http://127.0.0.1:8081/auth/sign-in/" `
    -Headers $headers `
    -Body @{
        email = $adminEmail
        password = $adminPassword
        csrfmiddlewaretoken = $csrfToken
    } `
    -WebSession $session `
    -UseBasicParsing `
    -TimeoutSec 30 | Out-Null
$csrfToken = (Invoke-RestMethod `
    -Uri "http://127.0.0.1:8081/auth/get-csrf-token/" `
    -WebSession $session `
    -TimeoutSec 10).csrf_token
$headers = @{
    "X-CSRFToken" = $csrfToken
    "Referer" = "http://127.0.0.1:8081/"
}

$workspaceSlug = "automation"
$projectResponse = Invoke-RestMethod `
    -Uri "http://127.0.0.1:8081/api/workspaces/$workspaceSlug/projects/" `
    -WebSession $session `
    -TimeoutSec 20
$projects = @(Expand-ResponseItems $projectResponse)
$project = $projects | Where-Object { $_.identifier -eq $ProjectIdentifier } | Select-Object -First 1
if ($null -eq $project) { throw "Plane project $ProjectIdentifier was not found" }

$stateResponse = Invoke-RestMethod `
    -Uri "http://127.0.0.1:8081/api/workspaces/$workspaceSlug/projects/$($project.id)/states/" `
    -WebSession $session `
    -TimeoutSec 20
$states = @(Expand-ResponseItems $stateResponse)
$targetState = $states | Where-Object { $_.name -eq $StateName } | Select-Object -First 1
if ($null -eq $targetState) { throw "Plane state '$StateName' was not found" }

$effectiveDescription = $Description
if ($StateName -eq "Testing") {
    $effectiveDescription += "`n`nAutomation implementation ref: $ImplementationRef"
}
$encodedDescription = [Net.WebUtility]::HtmlEncode($effectiveDescription)
try {
    $issue = Invoke-RestMethod `
        -Method Post `
        -Uri "http://127.0.0.1:8081/api/workspaces/$workspaceSlug/projects/$($project.id)/issues/" `
        -Headers $headers `
        -ContentType "application/json" `
        -Body (@{
            name = $Title
            description_html = "<p>$encodedDescription</p>"
            state_id = $targetState.id
        } | ConvertTo-Json -Compress) `
        -WebSession $session `
        -TimeoutSec 30
} catch {
    $details = $_.ErrorDetails.Message
    $response = $_.Exception.Response
    if ([string]::IsNullOrWhiteSpace($details) -and $null -ne $response) {
        $reader = New-Object IO.StreamReader($response.GetResponseStream())
        try { $details = $reader.ReadToEnd() } finally { $reader.Dispose() }
    }
    throw "Plane rejected the ticket: $details"
}

$workflow = $null
$deadline = (Get-Date).AddSeconds(60)
do {
    Start-Sleep -Seconds 2
    $workflowResponse = Invoke-RestMethod -Uri "http://127.0.0.1:8080/v1/workflows" -TimeoutSec 10
    $workflows = @(Expand-ResponseItems $workflowResponse)
    $workflow = $workflows | Where-Object { $_.trigger.data.ticket.id -eq $issue.id } | Select-Object -First 1
} while ($null -eq $workflow -and (Get-Date) -lt $deadline)

if ($null -eq $workflow) {
    throw "Plane created issue $($issue.id), but its event did not create a workflow"
}

Write-Output "Plane ticket created: $ProjectIdentifier-$($issue.sequence_id)"
Write-Output "Workflow queued: $($workflow.id) ($($workflow.status))"
Write-Output "Plane state: $StateName"
