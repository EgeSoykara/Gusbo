# Windows Scheduler Setup

## Files
- `scripts/run_marketplace_lifecycle.bat`
- `scripts/register_marketplace_lifecycle_task.ps1`
- `scripts/unregister_marketplace_lifecycle_task.ps1`

## Create/Update Scheduled Task
Run in PowerShell (as Admin if needed):

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\register_marketplace_lifecycle_task.ps1 -IntervalMinutes 1
```

Optional:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\register_marketplace_lifecycle_task.ps1 -IntervalMinutes 1 -RunAsSystem
```

Dry run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\register_marketplace_lifecycle_task.ps1 -IntervalMinutes 1 -WhatIf
```

## Remove Scheduled Task

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\unregister_marketplace_lifecycle_task.ps1
```

Dry run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\unregister_marketplace_lifecycle_task.ps1 -WhatIf
```
