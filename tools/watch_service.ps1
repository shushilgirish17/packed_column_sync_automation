<#
watch_service.ps1
-----------------
Consumer for the local handoff folder. It processes jobs from sqs_pull.py and
runs the full UNICORN sync (search -> create/update -> verify) via
sync_column.ps1. Jobs are processed ONE AT A TIME (UNICORN has a single
desktop).

Folder layout (created on first run under WATCH_DIR):
    incoming/    <stem>.job.json + <stem>.zip   (a producer drops here)
    processing/  the job currently being run
    done/        succeeded
    failed/      failed (inspect the matching log)
    logs/        <stem>.log per job

Run it in a console for the demo:
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\watch_service.ps1

  -Once          process whatever is waiting, then exit (good for a scripted demo)
  -IntervalSec   poll interval when looping (default 5)

The SQS poller writes verified jobs to this folder. Keep the poller and this
consumer running separately.
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

function Report-Completion($job, $code, $log) {
    # Tell the Benchling canvas that queued this job what actually happened in UNICORN - the
    # canvas only knows "Queued" until this arrives (see canvas_handler.report_column_result on
    # the Lambda side). No-op for jobs with no canvasId (manual/local runs) or when reporting
    # isn't configured (LAMBDA_FUNCTION_NAME unset) - never fails the job itself.
    if (-not $job.canvasId) { return }
    # Prefer the function that actually created this job (both tenants currently share one SQS
    # queue, so jobs interleave) - fall back to the static config for older jobs without it.
    $fn = if ($job.lambdaFunctionName) { $job.lambdaFunctionName } else { $cfg.LambdaFunctionName }
    if (-not $fn) {
        Log "  (no Lambda function known for canvas $($job.canvasId) - skipping report; set LAMBDA_FUNCTION_NAME)"
        return
    }

    $result  = if ($code -eq 0) { 'Synced' } else { 'Failed' }
    $message = if ($code -eq 0) { 'Synced to UNICORN' } else { "UNICORN sync failed (exit $code) - see log" }
    $payload = @{
        action        = 'report'
        canvas_id     = $job.canvasId
        column_id     = $job.benchlingEntityId
        registry_id   = $job.benchlingRegistryId
        column_name   = $job.columnName
        result        = $result
        message       = $message
        column_type_name = $job.columnName
        warnings      = $job.warnings
    } | ConvertTo-Json -Compress -Depth 5

    $payloadFile = [IO.Path]::GetTempFileName()
    $outFile     = [IO.Path]::GetTempFileName()
    try {
        [IO.File]::WriteAllText($payloadFile, $payload, [Text.Encoding]::UTF8)
        $awsArgs = @('lambda', 'invoke', '--function-name', $fn,
                     '--payload', "fileb://$payloadFile", '--cli-binary-format', 'raw-in-base64-out')
        if ($cfg.AwsProfile) { $awsArgs += @('--profile', $cfg.AwsProfile) }
        if ($cfg.AwsRegion)  { $awsArgs += @('--region', $cfg.AwsRegion) }
        $awsArgs += $outFile

        $prevEAP = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        $out = & aws @awsArgs 2>&1
        $awsCode = $LASTEXITCODE
        $ErrorActionPreference = $prevEAP
        $resp = if (Test-Path $outFile) { Get-Content $outFile -Raw } else { '' }
        "`n[report-completion exit $awsCode] $out`nresponse: $resp" | Out-File -FilePath $log -Append -Encoding utf8

        if ($awsCode -eq 0) { Log "  reported '$result' to canvas $($job.canvasId)" }
        else { Log "  ! failed to report '$result' to canvas $($job.canvasId) (aws exit $awsCode) - see log" }
    } catch {
        Log "  ! failed to report completion to canvas $($job.canvasId): $($_.Exception.Message)"
        "`n[report-completion crashed] $($_.Exception.Message)" | Out-File -FilePath $log -Append -Encoding utf8
    } finally {
        Remove-Item $payloadFile, $outFile -Force -ErrorAction SilentlyContinue
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
    # NB: run child scripts with EAP=Continue locally - a child writing to
    # stderr surfaces as a NativeCommandError that, under this script's Stop
    # preference, would otherwise KILL the whole watcher on one bad job.
    Log "  ensuring UNICORN ready..."
    $prevEAP = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    & powershell -NoProfile -ExecutionPolicy Bypass -File $ensureScript 2>&1 |
        Out-File -FilePath $log -Encoding utf8
    $ensureCode = $LASTEXITCODE
    $ErrorActionPreference = $prevEAP
    if ($ensureCode -ne 0) {
        "`n[ensure_unicorn failed: exit $ensureCode]" | Out-File -FilePath $log -Append -Encoding utf8
        Log "  FAILED (UNICORN not ready) -> failed/ (log: $log)"
        Report-Completion $job $ensureCode $log
        Move-Pair $procJob $job.zip $failed
        return
    }

    $args = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $syncScript,
              '-ColumnName', $job.columnName)
    if ($job.set)  { $args += @('-Set', $job.set) }
    if ($procZip)  { $args += @('-ImportZip', $procZip) }

    Log "  running sync_column..."
    $prevEAP = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    $out = & powershell @args 2>&1
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prevEAP
    $out | Out-File -FilePath $log -Append -Encoding utf8
    "`n[exit $code]" | Out-File -FilePath $log -Append -Encoding utf8

    Report-Completion $job $code $log

    if ($code -eq 0) {
        Log "  OK -> done/ (log: $log)"
        Move-Pair $procJob $job.zip $done
    } else {
        Log "  FAILED (exit $code) -> failed/ (log: $log)"
        Move-Pair $procJob $job.zip $failed
    }
}

function Invoke-Sweep {
    # Process every job currently waiting, oldest first. Each job is isolated:
    # a crash in one must never take down the watcher (it has to keep serving).
    $jobs = Get-ChildItem -Path $incoming -Filter '*.job.json' -ErrorAction SilentlyContinue | Sort-Object Name
    foreach ($j in $jobs) {
        try { Process-Job $j.FullName }
        catch {
            Log "  ! job '$($j.Name)' crashed: $($_.Exception.Message)"
            try { if (Test-Path $j.FullName) { Move-Item $j.FullName $failed -Force } } catch {}
        }
    }
}

function Recover-Processing {
    # A job left in processing/ means a previous run was interrupted mid-job
    # (e.g. the watcher was killed by an undismissed dialog). Requeue it to
    # incoming/ so it is retried. The sync is idempotent - if the column was
    # already imported it takes the safe Edit path - and the per-job try/catch
    # now routes a repeat crash to failed/ instead of killing the watcher.
    $orphans = Get-ChildItem -Path $processing -Filter '*.job.json' -ErrorAction SilentlyContinue
    foreach ($o in $orphans) {
        $zip = $null
        try { $zip = (Get-Content $o.FullName -Raw | ConvertFrom-Json).zip } catch {}
        Log "recovering interrupted job '$($o.BaseName)' from processing/ -> incoming/ (will retry)"
        Move-Pair $o.FullName $zip $incoming
    }
}

Log "watch_service started - watching $incoming"
Log "(sync target: $($cfg.UnicornProcess) / '$($cfg.ColumnDialogTitle)', WinAppDriver $($cfg.WinAppDriverUrl))"

# Requeue anything left mid-flight by a previous interrupted run, then handle
# everything already sitting in the folder.
Recover-Processing
Invoke-Sweep

if ($Once) { Log "done (single pass)"; return }

# Event-driven: block until a *.job.json is created, then process. The
# IntervalSec timeout doubles as a periodic safety sweep so nothing is ever
# missed if an event is lost. Producers write the .zip *before* the
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
    try { Invoke-Sweep }
    catch { Log "  ! sweep error (continuing): $($_.Exception.Message)" }
}
