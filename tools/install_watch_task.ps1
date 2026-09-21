<#
install_watch_task.ps1
----------------------
Register (or remove) the watch_service.ps1 watcher as an auto-starting
background task, so the automation runs continuously and fires whenever
benchling_pull.py drops a new job in the watch folder.

WHY A SCHEDULED TASK, NOT A WINDOWS SERVICE
===========================================
A classic Windows Service runs in Session 0, which is isolated from every
interactive desktop. UNICORN's UI lives in the logged-on interactive session
(SessionId >= 1), so a Session-0 service physically cannot see or click it —
there is no flag that fixes this. The supported way to run UI automation
unattended is a **Scheduled Task that runs in the interactive session**:

  - Trigger  : At log on of the automation account
  - Principal: that account, LogonType = Interactive, RunLevel = Highest
  - Settings : restart on failure, no time limit, single instance

The host must be a dedicated machine kept logged on and unlocked (UIA stops
on a locked/secure desktop). See docs / memory for the deployment checklist.

Usage (from an ELEVATED PowerShell — registering a task needs admin):
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\install_watch_task.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\install_watch_task.ps1 -Uninstall
#>

param(
    [string]$TaskName = 'UnicornColumnSyncWatcher',
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$watcher = Join-Path $PSScriptRoot 'watch_service.ps1'

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        "Removed scheduled task '$TaskName'."
    } else {
        "No scheduled task named '$TaskName' found."
    }
    return
}

if (-not (Test-Path $watcher)) { throw "watch_service.ps1 not found at $watcher" }

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
    '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass ' +
    "-File `"$watcher`"")

# At logon of the current (automation) account.
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# Interactive logon is what lets the task drive UNICORN's UI. Highest run level
# because WinAppDriver / UIA generally need elevation.
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
    -LogonType Interactive -RunLevel Highest

# Keep it alive: restart on failure, run indefinitely, never a second copy.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

"Registered scheduled task '$TaskName'."
"  runs: $watcher"
"  as  : $env:USERNAME (interactive, highest)"
"  when: at logon, restarts on failure, single instance"
""
"Start it now without logging off:   Start-ScheduledTask -TaskName '$TaskName'"
"Check it:                            Get-ScheduledTask -TaskName '$TaskName'"
"Stop it:                             Stop-ScheduledTask  -TaskName '$TaskName'"
"Remove it:                           tools\install_watch_task.ps1 -Uninstall"
