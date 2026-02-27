[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$TaskName = "UstaBul-MarketplaceLifecycle"
)

$ErrorActionPreference = "Stop"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if (-not $task) {
    Write-Host "Task not found: $TaskName"
    exit 0
}

if ($PSCmdlet.ShouldProcess($TaskName, "Unregister scheduled task")) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Scheduled task removed: $TaskName"
}
