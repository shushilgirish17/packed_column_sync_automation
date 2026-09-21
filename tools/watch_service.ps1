<#
watch_service.ps1
-----------------
Background watcher - the on-prem "agent" for the demo. It monitors a local
watch folder for jobs produced by benchling_pull.py and, for each one, runs
the full UNICORN sync (search -> create/update -> verify) via sync_column.ps1.

This is the local stand-in for the SQS/S3 consumer; the folder replaces the
queue. Jobs are processed ONE AT A TIME (UNICORN has a single desktop).

Folder layout (created on first run under WATCH_DIR):
    incoming/    <stem>.job.json + <stem>.zip   (benchling_pull drops here)
    processing/  the job currently being run
    done/        succeeded
    failed/      failed (inspect the matching log)
    logs/        <stem>.log per job

Run it in a console for the demo:
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\watch_service.ps1

  -Once          process whatever is waiting, then exit (good for a scripted demo)
  -IntervalSec   poll interval when looping (default 5)

For production this same loop is what an Automation Anywhere bot / Task
Scheduler job would run; nothing else changes.
#>

param(
    [switch]$Once,
    [int]$IntervalSec = 5
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
. (Join-Path $root 'lib\env.ps1')
$cfg = Get-ProjectConfig

$watch      = $cfg.WatchDir
$incoming   = Join-Path $watch 'incoming'
$processing = Join-Path $watch 'processing'
$done       = Join-Path $watch 'done'
$failed     = Join-Path $watch 'failed'
$logs       = Join-Path $watch 'logs'
foreach ($d in @($incoming, $processing, $done, $failed, $logs)) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}

$syncScript   = Join-Path $PSScriptRoot 'sync_column.ps1'
$ensureScript = Join-Path $PSScriptRoot 'ensure_unicorn.ps1'

function Log($msg) {
    $line = "{0}  {1}" -f (Get-Date -Format 'HH:mm:ss'), $msg
    Write-Host $line
}

function Move-Pair($jobFile, $zipName, $destDir) {
    Move-Item -Path $jobFile -Destination $destDir -Force
    if ($zipName) {
        $zipSrc = Join-Path (Split-Path $jobFile -Parent) $zipName
        if (Test-Path $zipSrc) { Move-Item -Path $zipSrc -Destination $destDir -Force }
    }
}

function Process-Job($jobFile) {
    $stem = [IO.Path]::GetFileNameWithoutExtension($jobFile) -replace '\.job$', ''
    $log  = Join-Path $logs "$stem.log"
    try {
        $job = Get-Content $jobFile -Raw | ConvertFrom-Json
    } catch {
        Log "  ! bad job file, moving to failed: $jobFile"
        Move-Item -Path $jobFile -Destination $failed -Force
        return
    }

    Log "job '$stem' -> column '$($job.columnName)'"

    # Claim: move job + zip into processing/ before running.
    Move-Pair $jobFile $job.zip $processing
    $procJob = Join-Path $processing (Split-Path $jobFile -Leaf)
    $procZip = if ($job.zip) { Join-Path $processing $job.zip } else { $null }

    # Ensure UNICORN is up, logged in, and Column Handling is open before the
    # sync. Idempotent and fast when it already is.
    Log "  ensuring UNICORN ready..."
    & powershell -NoProfile -ExecutionPolicy Bypass -File $ensureScript 2>&1 |
        Out-File -FilePath $log -Encoding utf8
    if ($LASTEXITCODE -ne 0) {
        "`n[ensure_unicorn failed: exit $LASTEXITCODE]" | Out-File -FilePath $log -Append -Encoding utf8
        Log "  FAILED (UNICORN not ready) -> failed/ (log: $log)"
        Move-Pair $procJob $job.zip $failed
        return
    }

    $args = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $syncScript,
              '-ColumnName', $job.columnName)
    if ($job.set)  { $args += @('-Set', $job.set) }
    if ($procZip)  { $args += @('-ImportZip', $procZip) }

    Log "  running sync_column..."
    $out = & powershell @args 2>&1
    $code = $LASTEXITCODE
    $out | Out-File -FilePath $log -Append -Encoding utf8
    "`n[exit $code]" | Out-File -FilePath $log -Append -Encoding utf8

    if ($code -eq 0) {
        Log "  OK -> done/ (log: $log)"
        Move-Pair $procJob $job.zip $done
    } else {
        Log "  FAILED (exit $code) -> failed/ (log: $log)"
        Move-Pair $procJob $job.zip $failed
    }
}

function Invoke-Sweep {
    # Process every job currently waiting, oldest first.
    $jobs = Get-ChildItem -Path $incoming -Filter '*.job.json' -ErrorAction SilentlyContinue | Sort-Object Name
    foreach ($j in $jobs) { Process-Job $j.FullName }
}

Log "watch_service started - watching $incoming"
Log "(sync target: $($cfg.UnicornProcess) / '$($cfg.ColumnDialogTitle)', WinAppDriver $($cfg.WinAppDriverUrl))"

# Handle anything already sitting in the folder (e.g. produced while we were down).
Invoke-Sweep

if ($Once) { Log "done (single pass)"; return }

# Event-driven: block until a *.job.json is created, then process. The
# IntervalSec timeout doubles as a periodic safety sweep so nothing is ever
# missed if an event is lost. benchling_pull writes the .zip *before* the
# .job.json, so by the time we react the payload is already complete; the
# short settle covers any final flush.
$fsw = New-Object System.IO.FileSystemWatcher
$fsw.Path   = $incoming
$fsw.Filter = '*.job.json'
$fsw.NotifyFilter = [System.IO.NotifyFilters]::FileName -bor [System.IO.NotifyFilters]::LastWrite
Log "waiting for new jobs (event-driven; safety sweep every ${IntervalSec}s)..."

while ($true) {
    $chg = $fsw.WaitForChanged([System.IO.WatcherChangeTypes]::Created,
                               [Math]::Max(1, $IntervalSec) * 1000)
    if (-not $chg.TimedOut) {
        Log "new job detected: $($chg.Name)"
        Start-Sleep -Milliseconds 700   # let the producer finish writing
    }
    Invoke-Sweep
}
