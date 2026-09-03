param(
    [Parameter(Mandatory = $true)]
    [string]$TicketId,
    [Parameter(Mandatory = $true)]
    [string]$Summary,
    [string]$Repository = "harnes/payments-api",
    [string]$DefaultBranch = "main",
    [string]$CloneUrl = "",
    [string]$OrchestratorUrl = "http://127.0.0.1:8080",
    [int]$TimeoutSeconds = 1200
)

$ErrorActionPreference = "Stop"
if ($Repository -notmatch '^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$') {
    throw "Repository must use owner/name format"
}
if ([string]::IsNullOrWhiteSpace($CloneUrl)) {
    $CloneUrl = "http://gitea:3000/$Repository.git"
}
$safeTicket = ($TicketId -replace '[^A-Za-z0-9._-]', '-')
$eventId = "ticket-$safeTicket-$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())"
$payload = @{
    source = "plane"
    event = "issue.ready_for_development"
    event_id = $eventId
    data = @{
        ticket = @{
            id = $TicketId
            summary = $Summary
            url = ""
        }
        repository = @{
            full_name = $Repository
            default_branch = $DefaultBranch
            clone_url = $CloneUrl
        }
    }
}

$workflow = Invoke-RestMethod `
    -Method Post `
    -Uri "$OrchestratorUrl/v1/triggers" `
    -ContentType "application/json" `
    -Body ($payload | ConvertTo-Json -Depth 6)

$deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
do {
    Start-Sleep -Seconds 5
    $workflow = Invoke-RestMethod -Uri "$OrchestratorUrl/v1/workflows/$($workflow.id)"
    Write-Host "workflow=$($workflow.id) status=$($workflow.status) step=$($workflow.current_step)"
    if ($workflow.status -in @("WAITING", "COMPLETED", "FAILED", "CANCELLED")) {
        break
    }
} while ([DateTimeOffset]::UtcNow -lt $deadline)

if ($workflow.status -eq "WAITING") {
    [pscustomobject]@{
        Workflow = $workflow.id
        Status = $workflow.status
        PullRequest = $workflow.pending_review.url
        Next = "Approve or reject the pull request in Gitea"
    }
    exit 0
}
if ($workflow.status -eq "COMPLETED") {
    $workflow
    exit 0
}
if ($workflow.status -in @("FAILED", "CANCELLED")) {
    $workflow | ConvertTo-Json -Depth 8
    exit 1
}
throw "Workflow did not reach a terminal or review state within $TimeoutSeconds seconds"
