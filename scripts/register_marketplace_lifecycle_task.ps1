[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$TaskName = "UstaBul-MarketplaceLifecycle",
    [ValidateRange(1, 60)]
    [int]$IntervalMinutes = 1,
    [string]$PythonExe = "",
    [switch]$RunAsSystem
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$batPath = Join-Path $scriptDir "run_marketplace_lifecycle.bat"

if (-not (Test-Path -Path $batPath -PathType Leaf)) {
    throw "Batch file not found: $batPath"
}

$quotedBat = '"' + $batPath + '"'
if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    $command = $quotedBat
} else {
    $command = 'set PYTHON_EXE=' + $PythonExe + '&& ' + $quotedBat
}

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $command"
$startAt = (Get-Date).AddMinutes(1)
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At $startAt `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

$description = "Runs Django marketplace_lifecycle command every $IntervalMinutes minute(s)."

$registerArgs = @{
    TaskName    = $TaskName
    Action      = $action
    Trigger     = $trigger
    Settings    = $settings
    Description = $description
    Force       = $true
}

if ($RunAsSystem.IsPresent) {
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $registerArgs["Principal"] = $principal
}

if ($PSCmdlet.ShouldProcess($TaskName, "Register scheduled task")) {
    Register-ScheduledTask @registerArgs | Out-Null
    Write-Host "Scheduled task created/updated: $TaskName"
    Write-Host "Batch: $batPath"
    Write-Host "Interval: $IntervalMinutes minute(s)"
    if ($RunAsSystem.IsPresent) {
        Write-Host "Run as: SYSTEM"
    } else {
        Write-Host "Run as: current user context"
    }
}
