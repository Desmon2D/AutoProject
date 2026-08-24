param(
    [string]$ProjectIdentifier = "PAY",
    [string]$Repository = "harnes/payments-api"
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$envFile = Join-Path $root ".env"
$docker = "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin\docker.exe"
if (-not (Test-Path -LiteralPath $docker)) {
    throw "Docker CLI not found: $docker"
}

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

function Expand-ResponseItems($Value) {
    foreach ($outer in @($Value)) {
        if ($outer -is [Array]) {
            foreach ($inner in $outer) { Write-Output $inner }
        } else {
            Write-Output $outer
        }
    }
}

$webhookSecret = Get-DotEnvValue "PLANE_WEBHOOK_SECRET"
if ([string]::IsNullOrWhiteSpace($webhookSecret)) {
    $secretBytes = New-Object byte[] 32
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $random.GetBytes($secretBytes) } finally { $random.Dispose() }
    $webhookSecret = -join ($secretBytes | ForEach-Object { $_.ToString("x2") })
}
$adminEmail = Get-DotEnvValue "PLANE_ADMIN_EMAIL"
if ([string]::IsNullOrWhiteSpace($adminEmail)) {
    $adminEmail = "admin@local.test"
}
$adminPassword = Get-DotEnvValue "PLANE_ADMIN_PASSWORD"
if ([string]::IsNullOrWhiteSpace($adminPassword)) {
    $passwordBytes = New-Object byte[] 20
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $random.GetBytes($passwordBytes) } finally { $random.Dispose() }
    $adminPassword = "Plane-Aa1!" + (-join ($passwordBytes | ForEach-Object { $_.ToString("x2") }))
}

Set-DotEnvValue "PLANE_BASE_URL" "http://plane"
Set-DotEnvValue "PLANE_WORKSPACE_SLUG" "automation"
Set-DotEnvValue "PLANE_WEBHOOK_SECRET" $webhookSecret
Set-DotEnvValue "PLANE_READY_STATE_NAMES" "Ready for development"
Set-DotEnvValue "PLANE_IN_DEVELOPMENT_STATE_NAMES" "In development"
Set-DotEnvValue "PLANE_DEVELOPMENT_REVIEW_STATE_NAMES" "Development review"
Set-DotEnvValue "PLANE_TESTING_STATE_NAMES" "Testing"
Set-DotEnvValue "PLANE_ADMIN_EMAIL" $adminEmail
Set-DotEnvValue "PLANE_ADMIN_PASSWORD" $adminPassword
Set-DotEnvValue "PLANE_PROJECT_REPOSITORIES" (@{
    $ProjectIdentifier = $Repository
} | ConvertTo-Json -Compress)

Set-Location $root
& $docker compose stop worker | Out-Null
& $docker compose --profile gitea stop gitea | Out-Null
& $docker compose --profile swirl stop swirl swirl-redis | Out-Null
& $docker compose --profile lite stop swirl-lite | Out-Null

& $docker compose --profile plane build plane
if ($LASTEXITCODE -ne 0) { throw "Cannot build the low-memory Plane image" }
& $docker compose --profile plane up -d plane orchestrator dashboard
if ($LASTEXITCODE -ne 0) { throw "Cannot start Plane" }

$deadline = (Get-Date).AddMinutes(5)
do {
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:8081/api/instances/" -UseBasicParsing -TimeoutSec 3
        if ($response.StatusCode -eq 200) { break }
    }
    catch { }
    Start-Sleep -Seconds 2
} while ((Get-Date) -lt $deadline)
if ($response.StatusCode -ne 200) { throw "Plane did not become ready" }

$instanceState = $response.Content | ConvertFrom-Json
if (-not $instanceState.instance.is_setup_done) {
    $setupSession = New-Object Microsoft.PowerShell.Commands.WebRequestSession
    $csrfResponse = Invoke-RestMethod `
        -Uri "http://127.0.0.1:8081/auth/get-csrf-token/" `
        -WebSession $setupSession `
        -TimeoutSec 10
    $csrfToken = $csrfResponse.csrf_token
    if ([string]::IsNullOrWhiteSpace($csrfToken)) {
        throw "Plane did not return a CSRF token"
    }
    Invoke-WebRequest `
        -Method Post `
        -Uri "http://127.0.0.1:8081/api/instances/admins/sign-up/" `
        -Headers @{
            "X-CSRFToken" = $csrfToken
            "Referer" = "http://127.0.0.1:8081/god-mode/"
        } `
        -Body @{
            email = $adminEmail
            password = $adminPassword
            first_name = "Automation"
            last_name = "Admin"
            company_name = "Local Development"
            is_telemetry_enabled = "False"
            csrfmiddlewaretoken = $csrfToken
        } `
        -WebSession $setupSession `
        -UseBasicParsing `
        -TimeoutSec 30 | Out-Null
    $instanceState = Invoke-RestMethod `
        -Uri "http://127.0.0.1:8081/api/instances/" `
        -TimeoutSec 10
    if (-not $instanceState.instance.is_setup_done) {
        throw "Plane administrator setup did not complete"
    }
}

$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$csrfResponse = Invoke-RestMethod `
    -Uri "http://127.0.0.1:8081/auth/get-csrf-token/" `
    -WebSession $session `
    -TimeoutSec 10
$csrfToken = $csrfResponse.csrf_token
$headers = @{
    "X-CSRFToken" = $csrfToken
    "Referer" = "http://127.0.0.1:8081/god-mode/"
}
Invoke-WebRequest `
    -Method Post `
    -Uri "http://127.0.0.1:8081/api/instances/admins/sign-in/" `
    -Headers $headers `
    -Body @{
        email = $adminEmail
        password = $adminPassword
        csrfmiddlewaretoken = $csrfToken
    } `
    -WebSession $session `
    -UseBasicParsing `
    -TimeoutSec 30 | Out-Null

# Django changes the CSRF token after authentication.
$csrfResponse = Invoke-RestMethod `
    -Uri "http://127.0.0.1:8081/auth/get-csrf-token/" `
    -WebSession $session `
    -TimeoutSec 10
$csrfToken = $csrfResponse.csrf_token
$headers = @{
    "X-CSRFToken" = $csrfToken
    "Referer" = "http://127.0.0.1:8081/god-mode/"
}

$workspaceSlug = "automation"
$workspaceList = Invoke-RestMethod `
    -Uri "http://127.0.0.1:8081/api/instances/workspaces/?per_page=10" `
    -WebSession $session `
    -TimeoutSec 20
$workspaceItems = if ($null -ne $workspaceList.results) {
    @($workspaceList.results)
} else {
    @($workspaceList)
}
$workspace = $workspaceItems | Where-Object { $_.slug -eq $workspaceSlug } | Select-Object -First 1
if ($null -eq $workspace) {
    $workspace = Invoke-RestMethod `
        -Method Post `
        -Uri "http://127.0.0.1:8081/api/instances/workspaces/" `
        -Headers $headers `
        -Body @{
            name = "Automation"
            slug = $workspaceSlug
            company_role = "Engineering"
            csrfmiddlewaretoken = $csrfToken
        } `
        -WebSession $session `
        -TimeoutSec 30
}

# Project and webhook endpoints use the regular application session,
# which is separate from the instance-administration session.
$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$csrfResponse = Invoke-RestMethod `
    -Uri "http://127.0.0.1:8081/auth/get-csrf-token/" `
    -WebSession $session `
    -TimeoutSec 10
$csrfToken = $csrfResponse.csrf_token
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
$csrfResponse = Invoke-RestMethod `
    -Uri "http://127.0.0.1:8081/auth/get-csrf-token/" `
    -WebSession $session `
    -TimeoutSec 10
$csrfToken = $csrfResponse.csrf_token
$headers = @{
    "X-CSRFToken" = $csrfToken
    "Referer" = "http://127.0.0.1:8081/"
}

$apiToken = Get-DotEnvValue "PLANE_API_TOKEN"
if ([string]::IsNullOrWhiteSpace($apiToken)) {
    $tokenResponse = Invoke-RestMethod `
        -Method Post `
        -Uri "http://127.0.0.1:8081/api/users/api-tokens/" `
        -Headers $headers `
        -ContentType "application/json" `
        -Body (@{
            label = "AutoProject orchestrator"
            description = "Local workflow result synchronization"
        } | ConvertTo-Json -Compress) `
        -WebSession $session `
        -TimeoutSec 30
    $apiToken = $tokenResponse.token
    if ([string]::IsNullOrWhiteSpace($apiToken)) {
        throw "Plane did not return an API token"
    }
    Set-DotEnvValue "PLANE_API_TOKEN" $apiToken
}

$projectList = Invoke-RestMethod `
    -Uri "http://127.0.0.1:8081/api/workspaces/$workspaceSlug/projects/" `
    -WebSession $session `
    -TimeoutSec 20
$projectItems = if ($null -ne $projectList.results) {
    @($projectList.results)
} else {
    @(Expand-ResponseItems $projectList)
}
$project = $projectItems | Where-Object { $_.identifier -eq $ProjectIdentifier } | Select-Object -First 1
if ($null -eq $project) {
    $project = Invoke-RestMethod `
        -Method Post `
        -Uri "http://127.0.0.1:8081/api/workspaces/$workspaceSlug/projects/" `
        -Headers $headers `
        -ContentType "application/json" `
        -Body (@{
            name = "Payments API"
            identifier = $ProjectIdentifier
        } | ConvertTo-Json -Compress) `
        -WebSession $session `
        -TimeoutSec 30
}
$projectRepositories = @{}
$projectRepositories[$ProjectIdentifier] = $Repository
$projectRepositories[[string]$project.id] = $Repository
Set-DotEnvValue "PLANE_PROJECT_REPOSITORIES" ($projectRepositories | ConvertTo-Json -Compress)

$stateUrl = "http://127.0.0.1:8081/api/workspaces/$workspaceSlug/projects/$($project.id)/states/"
$stateResponse = Invoke-RestMethod `
    -Uri $stateUrl `
    -WebSession $session `
    -TimeoutSec 20
$states = @(Expand-ResponseItems $stateResponse)
$readyState = $states | Where-Object { $_.name -eq "Ready for development" } | Select-Object -First 1
if ($null -eq $readyState) {
    $readyState = Invoke-RestMethod `
        -Method Post `
        -Uri $stateUrl `
        -Headers $headers `
        -ContentType "application/json" `
        -Body (@{
            name = "Ready for development"
            color = "#5E6AD2"
            group = "unstarted"
            description = "Ready for the automation worker"
            sequence = 15000
        } | ConvertTo-Json -Compress) `
        -WebSession $session `
        -TimeoutSec 30
}
Set-DotEnvValue "PLANE_READY_STATE_IDS" $readyState.id
$inDevelopmentState = $states | Where-Object { $_.name -eq "In development" } | Select-Object -First 1
if ($null -eq $inDevelopmentState) {
    $inDevelopmentState = Invoke-RestMethod `
        -Method Post `
        -Uri $stateUrl `
        -Headers $headers `
        -ContentType "application/json" `
        -Body (@{
            name = "In development"
            color = "#2563EB"
            group = "started"
            description = "Implementation is running"
            sequence = 75000
        } | ConvertTo-Json -Compress) `
        -WebSession $session `
        -TimeoutSec 30
}
Set-DotEnvValue "PLANE_IN_DEVELOPMENT_STATE_IDS" $inDevelopmentState.id
$developmentReviewState = $states | Where-Object { $_.name -eq "Development review" } | Select-Object -First 1
if ($null -eq $developmentReviewState) {
    $developmentReviewState = Invoke-RestMethod `
        -Method Post `
        -Uri $stateUrl `
        -Headers $headers `
        -ContentType "application/json" `
        -Body (@{
            name = "Development review"
            color = "#8B5CF6"
            group = "started"
            description = "Implementation branch is waiting for a human decision"
            sequence = 80000
        } | ConvertTo-Json -Compress) `
        -WebSession $session `
        -TimeoutSec 30
}
Set-DotEnvValue "PLANE_DEVELOPMENT_REVIEW_STATE_IDS" $developmentReviewState.id
$testingState = $states | Where-Object { $_.name -eq "Testing" } | Select-Object -First 1
if ($null -eq $testingState) {
    $testingState = Invoke-RestMethod `
        -Method Post `
        -Uri $stateUrl `
        -Headers $headers `
        -ContentType "application/json" `
        -Body (@{
            name = "Testing"
            color = "#F59E0B"
            group = "started"
            description = "Ready for automated test creation and execution"
            sequence = 25000
        } | ConvertTo-Json -Compress) `
        -WebSession $session `
        -TimeoutSec 30
}
Set-DotEnvValue "PLANE_TESTING_STATE_IDS" $testingState.id
$completedState = $states | Where-Object { $_.group -eq "completed" } | Select-Object -First 1
if ($null -eq $completedState) {
    $completedState = Invoke-RestMethod `
        -Method Post `
        -Uri $stateUrl `
        -Headers $headers `
        -ContentType "application/json" `
        -Body (@{
            name = "Done"
            color = "#16A34A"
            group = "completed"
            description = "Validated change was accepted"
            sequence = 35000
        } | ConvertTo-Json -Compress) `
        -WebSession $session `
        -TimeoutSec 30
}
Set-DotEnvValue "PLANE_COMPLETED_STATE_IDS" $completedState.id
$cancelledState = $states | Where-Object { $_.group -eq "cancelled" } | Select-Object -First 1
if ($null -eq $cancelledState) {
    $cancelledState = Invoke-RestMethod `
        -Method Post `
        -Uri $stateUrl `
        -Headers $headers `
        -ContentType "application/json" `
        -Body (@{
            name = "Cancelled"
            color = "#DC2626"
            group = "cancelled"
            description = "Validated change was rejected"
            sequence = 45000
        } | ConvertTo-Json -Compress) `
        -WebSession $session `
        -TimeoutSec 30
}
Set-DotEnvValue "PLANE_CANCELLED_STATE_IDS" $cancelledState.id

$webhookUrl = "http://orchestrator.local:8080/v1/webhooks/plane"
$webhookResponse = Invoke-RestMethod `
    -Uri "http://127.0.0.1:8081/api/workspaces/$workspaceSlug/webhooks/" `
    -WebSession $session `
    -TimeoutSec 20
$webhooks = @(Expand-ResponseItems $webhookResponse)
$staleWebhooks = $webhooks | Where-Object {
    $_.url -ne $webhookUrl -and $_.url -match "/v1/webhooks/plane$"
}
foreach ($staleWebhook in $staleWebhooks) {
    Invoke-RestMethod `
        -Method Delete `
        -Uri "http://127.0.0.1:8081/api/workspaces/$workspaceSlug/webhooks/$($staleWebhook.id)/" `
        -Headers $headers `
        -WebSession $session `
        -TimeoutSec 20 | Out-Null
}
$webhook = $webhooks | Where-Object { $_.url -eq $webhookUrl } | Select-Object -First 1
if ($null -eq $webhook) {
    $webhook = Invoke-RestMethod `
        -Method Post `
        -Uri "http://127.0.0.1:8081/api/workspaces/$workspaceSlug/webhooks/" `
        -Headers $headers `
        -ContentType "application/json" `
        -Body (@{
            url = $webhookUrl
            issue = $true
            is_active = $true
        } | ConvertTo-Json -Compress) `
        -WebSession $session `
        -TimeoutSec 30
} else {
    $webhook = Invoke-RestMethod `
        -Method Post `
        -Uri "http://127.0.0.1:8081/api/workspaces/$workspaceSlug/webhooks/$($webhook.id)/regenerate/" `
        -Headers $headers `
        -ContentType "application/json" `
        -Body "{}" `
        -WebSession $session `
        -TimeoutSec 30
}
if ([string]::IsNullOrWhiteSpace($webhook.secret_key)) {
    throw "Plane did not return the webhook secret"
}
Set-DotEnvValue "PLANE_WEBHOOK_SECRET" $webhook.secret_key
& $docker compose up -d --force-recreate orchestrator | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Cannot apply the Plane webhook secret to the orchestrator" }

Write-Output "Plane development mode is ready."
Write-Output "Plane: http://127.0.0.1:8081"
Write-Output "Admin: $adminEmail (password is stored in the ignored .env file)"
Write-Output "Workspace/project: $workspaceSlug/$ProjectIdentifier"
Write-Output "Project mapping: $ProjectIdentifier -> $Repository"
Write-Output "The workflow worker is stopped. After Plane delivers an event, run:"
Write-Output "  .\scripts\dev\start-low-memory.ps1 -WithGitea"
& $docker stats --no-stream --format "table {{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}"
