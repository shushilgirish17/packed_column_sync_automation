<#
sync_column.ps1
---------------
End-to-end sync of ONE column type into UNICORN - the whole flow in one
command, run sequentially with a clear step log. This is the reference
implementation of the agent's per-job logic (minus the S3/queue plumbing).

    Pre-flight  ->  Search  ->  Decide (update vs create)  ->  Edit | Import
                ->  Read-back  ->  Verify

Update an existing column type (-Set is "label=value" pairs, ';'-separated):
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\sync_column.ps1 `
        -ColumnName 'ZZTEST' -Set 'Default flow rate=2.5;Max flow rate=3.0'

Create a column type from a generated import file:
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\sync_column.ps1 `
        -ColumnName 'ZZTEST-BenchlingSync-DELETE-ME 1x10cm 7.9mL' `
        -ImportZip exports\ZZTEST_import.zip

Exit codes: 0 = success (and verified), 1 = failure, 2 = environment not ready.

Design notes
  - Proven mechanisms only: WinAppDriver (REST) for search + list select;
    .NET UI Automation for reads and ValuePattern edits; synthetic mouse for
    the owner-drawn main-dialog buttons (they have no HWND and sit at negative
    coordinates on a secondary monitor); SendMessage for message boxes.
  - Reuses lib/env.ps1 (config) and lib/win32_dialogs.ps1 (message boxes).
  - Idempotent: it searches first and decides, so re-running is safe.
#>

param(
    [Parameter(Mandatory)][string]$ColumnName,
    [string]$Set,          # "label=value;label=value" — fields to update
    [string]$ImportZip,
    [string]$Base
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
. (Join-Path $root 'lib\env.ps1')
. (Join-Path $root 'lib\win32_dialogs.ps1')
$cfg = Get-ProjectConfig
if (-not $Base) { $Base = $cfg.WinAppDriverUrl }

# Parse -Set into a label->value map. (Passed as a string so it survives
# `powershell -File`, which stringifies all arguments.)
$Changes = @{}
if ($Set) {
    foreach ($pair in $Set -split ';') {
        if ($pair -notmatch '=') { continue }
        $k, $v = $pair -split '=', 2
        $Changes[$k.Trim()] = $v.Trim()
    }
}

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;using System.Text;using System.Runtime.InteropServices;
public class Nav{public delegate bool EP(IntPtr h,IntPtr l);
[DllImport("user32.dll")]public static extern bool EnumWindows(EP cb,IntPtr l);
[DllImport("user32.dll",CharSet=CharSet.Unicode)]public static extern int GetWindowTextW(IntPtr h,StringBuilder s,int n);
[DllImport("user32.dll")]public static extern int GetWindowThreadProcessId(IntPtr h,out int pid);
[DllImport("user32.dll")]public static extern bool IsWindowVisible(IntPtr h);
[DllImport("user32.dll")]public static extern IntPtr GetWindow(IntPtr h,uint c);
[DllImport("user32.dll")]public static extern bool SetForegroundWindow(IntPtr h);
[DllImport("user32.dll")]public static extern bool SetCursorPos(int x,int y);
[DllImport("user32.dll")]public static extern bool GetCursorPos(out POINT p);
[DllImport("user32.dll")]public static extern void mouse_event(uint f,int dx,int dy,uint d,UIntPtr e);
public struct POINT{public int X;public int Y;}
public static IntPtr FindEq(int pid,string t){IntPtr f=IntPtr.Zero;EnumWindows((h,l)=>{int p;GetWindowThreadProcessId(h,out p);
 if(p==pid&&IsWindowVisible(h)){var s=new StringBuilder(400);GetWindowTextW(h,s,400);if(s.ToString()==t){f=h;return false;}}return true;},IntPtr.Zero);return f;}
public static IntPtr FindPre(int pid,string t){IntPtr f=IntPtr.Zero;EnumWindows((h,l)=>{int p;GetWindowThreadProcessId(h,out p);
 if(p==pid&&IsWindowVisible(h)){var s=new StringBuilder(400);GetWindowTextW(h,s,400);if(s.ToString().StartsWith(t)){f=h;return false;}}return true;},IntPtr.Zero);return f;}}
"@

$AE   = [System.Windows.Automation.AutomationElement]
$TS   = [System.Windows.Automation.TreeScope]
$COND = [System.Windows.Automation.Condition]
$VP   = [System.Windows.Automation.ValuePattern]::Pattern
$SIP  = [System.Windows.Automation.SelectionItemPattern]::Pattern

# --- small helpers ---------------------------------------------------------
$script:step = 0
function Step($msg) { $script:step++; Write-Host ("`n[{0}] {1}" -f $script:step, $msg) -ForegroundColor Cyan }
function Info($msg) { Write-Host "    $msg" }
function Die($code, $msg) { Write-Host "    x $msg" -ForegroundColor Red; exit $code }
function CtOf($el) { return ($el.Current.ControlType.ProgrammaticName -replace '^ControlType\.','') }

function Wad($method, $path, $body) {
    $args = @{ Method = $method; Uri = "$Base$path"; ContentType = 'application/json' }
    if ($null -ne $body) { $args.Body = ($body | ConvertTo-Json -Depth 6 -Compress) }
    return Invoke-RestMethod @args
}

function MouseClick($hwnd, $rect) {
    # Synthetic click at an element's screen rect - works at negative
    # coordinates and on owner-drawn buttons, unlike WinAppDriver's click.
    $cx = [int]($rect.X + $rect.Width / 2); $cy = [int]($rect.Y + $rect.Height / 2)
    $o = New-Object Nav+POINT; [void][Nav]::GetCursorPos([ref]$o)
    [void][Nav]::SetForegroundWindow($hwnd); Start-Sleep -Milliseconds 350
    [void][Nav]::SetCursorPos($cx, $cy); Start-Sleep -Milliseconds 120
    [Nav]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero); Start-Sleep -Milliseconds 60
    [Nav]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero); Start-Sleep -Milliseconds 200
    [void][Nav]::SetCursorPos($o.X, $o.Y)
}

function Find-Button($dlg, $autoId) {
    foreach ($e in $dlg.FindAll($TS::Descendants, $COND::TrueCondition)) {
        if ($e.Current.AutomationId -eq $autoId) { return $e }
    }
    return $null
}

function Read-Grids($dlg) {
    # label -> @{Value; Unit}, across all parameter grids.
    $out = @{}
    foreach ($g in $dlg.FindAll($TS::Descendants, $COND::TrueCondition)) {
        if ((CtOf $g) -ne 'DataGrid') { continue }
        foreach ($row in $g.FindAll($TS::Children, $COND::TrueCondition)) {
            if ($row.Current.Name -eq 'Top Row') { continue }
            $cells = @()
            foreach ($c in $row.FindAll($TS::Children, $COND::TrueCondition)) {
                if ((CtOf $c) -ne 'DataItem') { continue }
                $v = ''
                foreach ($p in $c.GetSupportedPatterns()) {
                    if ($p.ProgrammaticName -like 'Value*') { $v = $c.GetCurrentPattern($VP).Current.Value; if ($v -eq '(null)') { $v = '' } }
                }
                $cells += $v
            }
            if ($cells.Count -ge 2 -and $cells[0]) {
                $lbl = ($cells[0] -replace '^\*', '').Trim()
                if (-not $out.ContainsKey($lbl)) { $out[$lbl] = @{ Value = $cells[1]; Unit = $(if ($cells.Count -gt 2) { $cells[2] } else { '' }) } }
            }
        }
    }
    return $out
}

# ---------------------------------------------------------------------------
# 1. Pre-flight
# ---------------------------------------------------------------------------
Step "Pre-flight - UNICORN, WinAppDriver, provider health"
$u = Get-Process $cfg.UnicornProcess -ErrorAction SilentlyContinue
if (-not $u) { Die 2 "$($cfg.UnicornProcess) is not running." }
try { Wad GET '/status' $null | Out-Null } catch { Die 2 "WinAppDriver not reachable at $Base." }

$dlgH = [Nav]::FindEq($u.Id, $cfg.ColumnDialogTitle)
if ($dlgH -eq [IntPtr]::Zero) { Die 2 "'$($cfg.ColumnDialogTitle)' dialog is not open." }
$owner = [Nav]::GetWindow($dlgH, 4)
$dlg = $AE::FromHandle($dlgH)

# Provider health: direct children must include typed controls, read fast.
$sw = [Diagnostics.Stopwatch]::StartNew()
$kids = $dlg.FindAll($TS::Children, $COND::TrueCondition)
$typed = @($kids | Where-Object { (CtOf $_) -ne 'Pane' }).Count
if ($sw.ElapsedMilliseconds -gt 5000 -or $typed -eq 0) {
    Die 2 "UIA provider degraded ($($kids.Count) children, $typed typed, $($sw.ElapsedMilliseconds) ms). Restart UNICORN."
}
Info "healthy - $($u.ProcessName) pid=$($u.Id), $($kids.Count) children in $($sw.ElapsedMilliseconds) ms"

$sess = Wad POST '/session' @{ desiredCapabilities = @{ platformName = 'Windows'; appTopLevelWindow = ('0x{0:x}' -f [int64]$owner) } }
$sid = $sess.sessionId
try {

# ---------------------------------------------------------------------------
# 2. Ensure the Column Type Parameters tab is active
# ---------------------------------------------------------------------------
Step "Select the Column Type Parameters tab"
foreach ($t in $dlg.FindAll($TS::Descendants, $COND::TrueCondition)) {
    if ((CtOf $t) -eq 'TabItem' -and $t.Current.Name -eq 'Column Type Parameters') {
        if (-not $t.GetCurrentPattern($SIP).Current.IsSelected) { $t.GetCurrentPattern($SIP).Select(); Start-Sleep -Milliseconds 1000 }
    }
}
Info "active"

# ---------------------------------------------------------------------------
# 3. Search + decide
# ---------------------------------------------------------------------------
Step "Search for '$ColumnName'"
$se = (Wad POST "/session/$sid/element" @{ using = 'accessibility id'; value = 'txtSearch' }).value.ELEMENT
Wad POST "/session/$sid/element/$se/click" @{} | Out-Null
Wad POST "/session/$sid/element/$se/clear" @{} | Out-Null
Wad POST "/session/$sid/element/$se/value" @{ value = @($ColumnName) } | Out-Null
Start-Sleep -Milliseconds 1500

$match = $null
foreach ($it in @((Wad POST "/session/$sid/elements" @{ using = 'xpath'; value = '//ListItem' }).value)) {
    $nm = (Invoke-RestMethod -Method Get -Uri "$Base/session/$sid/element/$($it.ELEMENT)/attribute/Name").value
    if ($nm -like "*$ColumnName*") { $match = @{ Id = $it.ELEMENT; Name = $nm }; break }
}
$exists = [bool]$match
Info $(if ($exists) { "found: $($match.Name)" } else { "no match -> will create" })

if ($exists) {
    Wad POST "/session/$sid/element/$($match.Id)/click" @{} | Out-Null
    Start-Sleep -Milliseconds 800
    $panel = (Read-Grids $dlg)['Name'].Value
    if ($panel -notlike "*$ColumnName*") { Die 1 "Selection guard failed - panel shows '$panel'." }
}

# ---------------------------------------------------------------------------
# 4. Act - Edit (update) or Import (create)
# ---------------------------------------------------------------------------
if ($exists) {
    if ($Changes.Count -eq 0) { Step "Update"; Info "column exists and no -Set supplied -> nothing to change"; }
    else {
        Step "Update - Edit the column type"
        MouseClick $dlgH (Find-Button $dlg 'btnEditType').Current.BoundingRectangle
        $eh = [IntPtr]::Zero
        for ($i = 0; $i -lt 30 -and $eh -eq [IntPtr]::Zero; $i++) { Start-Sleep -Milliseconds 500; $eh = [Nav]::FindPre($u.Id, 'Edit Column type') }
        if ($eh -eq [IntPtr]::Zero) { Die 1 "Edit dialog did not open." }
        $ed = $AE::FromHandle($eh)

        foreach ($g in $ed.FindAll($TS::Descendants, $COND::TrueCondition)) {
            if ((CtOf $g) -ne 'DataGrid') { continue }
            foreach ($row in $g.FindAll($TS::Children, $COND::TrueCondition)) {
                if ($row.Current.Name -eq 'Top Row') { continue }
                $cs = @(); foreach ($c in $row.FindAll($TS::Children, $COND::TrueCondition)) { if ((CtOf $c) -eq 'DataItem') { $cs += $c } }
                if ($cs.Count -lt 2) { continue }
                $lbl = ($cs[0].GetCurrentPattern($VP).Current.Value -replace '^\*', '').Trim()
                if ($Changes.ContainsKey($lbl)) {
                    # NB: not $vp — PowerShell is case-insensitive and would
                    # clobber the $VP pattern constant.
                    $valPat = $cs[1].GetCurrentPattern($VP)
                    if ($valPat.Current.IsReadOnly) { Info "! '$lbl' read-only, skipped" }
                    else { $valPat.SetValue([string]$Changes[$lbl]); Info "set $lbl = $($Changes[$lbl])" }
                }
            }
        }
        MouseClick $eh (Find-Button $ed 'btnSave').Current.BoundingRectangle
        Start-Sleep -Milliseconds 2000
        if ([Nav]::FindPre($u.Id, 'Edit Column type') -ne [IntPtr]::Zero) { Die 1 "Edit dialog did not close after Save." }
        Info "saved"
    }
}
else {
    if (-not $ImportZip) { Die 1 "Column does not exist and no -ImportZip supplied." }
    $zip = if ([IO.Path]::IsPathRooted($ImportZip)) { $ImportZip } else { Join-Path $root $ImportZip }
    if (-not (Test-Path $zip)) { Die 1 "Import file not found: $zip" }
    Step "Create - Import $([IO.Path]::GetFileName($zip))"

    MouseClick $dlgH (Find-Button $dlg 'btnImportType').Current.BoundingRectangle
    $fd = [IntPtr]::Zero
    for ($i = 0; $i -lt 30 -and $fd -eq [IntPtr]::Zero; $i++) { Start-Sleep -Milliseconds 500; $fd = [Nav]::FindEq($u.Id, 'Import') }
    if ($fd -eq [IntPtr]::Zero) { Die 1 "Import file dialog did not open." }
    [void][Nav]::SetForegroundWindow($fd); Start-Sleep -Milliseconds 700
    [System.Windows.Forms.SendKeys]::SendWait('^a'); Start-Sleep -Milliseconds 150
    [System.Windows.Forms.SendKeys]::SendWait($zip); Start-Sleep -Milliseconds 300
    [System.Windows.Forms.SendKeys]::SendWait('{ENTER}'); Start-Sleep -Milliseconds 1500

    $sel = [Nav]::FindEq($u.Id, 'Import Column Type')
    if ($sel -ne [IntPtr]::Zero) {
        $ok = Find-Button ($AE::FromHandle($sel)) 'btnOK'
        MouseClick $sel $ok.Current.BoundingRectangle
        Start-Sleep -Milliseconds 1500
    }
    $done = Wait-UnicornDialog -TitleStartsWith 'Import' -TimeoutMs 8000   # success/info box
    if ($done -ne [IntPtr]::Zero) { [void](Invoke-DialogButton -Dialog $done -Text 'OK'); Info "import confirmed" }
    else { Info "no confirmation dialog (already applied)" }
}

# ---------------------------------------------------------------------------
# 5. Read-back
# ---------------------------------------------------------------------------
Step "Read-back"
$se = (Wad POST "/session/$sid/element" @{ using = 'accessibility id'; value = 'txtSearch' }).value.ELEMENT
Wad POST "/session/$sid/element/$se/clear" @{} | Out-Null
Wad POST "/session/$sid/element/$se/value" @{ value = @($ColumnName) } | Out-Null
Start-Sleep -Milliseconds 1200
$hit = @((Wad POST "/session/$sid/elements" @{ using = 'xpath'; value = '//ListItem' }).value) |
    Where-Object { (Invoke-RestMethod -Method Get -Uri "$Base/session/$sid/element/$($_.ELEMENT)/attribute/Name").value -like "*$ColumnName*" } |
    Select-Object -First 1
if (-not $hit) { Die 1 "Column not present after operation." }
Wad POST "/session/$sid/element/$($hit.ELEMENT)/click" @{} | Out-Null
Start-Sleep -Milliseconds 800
$final = Read-Grids $dlg
Info "reading: $($final['Name'].Value)"

# ---------------------------------------------------------------------------
# 6. Verify
# ---------------------------------------------------------------------------
Step "Verify"
$pass = $true
if ($exists -and $Changes.Count -gt 0) {
    foreach ($k in $Changes.Keys) {
        $want = [double]$Changes[$k]; $got = [double]$final[$k].Value
        $ok = ($got -eq $want); if (-not $ok) { $pass = $false }
        Write-Host ("    {0,-26} want={1,-6} got={2,-6} {3}" -f $k, $want, $got, $(if ($ok) { 'OK' } else { 'MISMATCH' }))
    }
    # derived-field cross-check
    $dia = [double]$final['Hardware diameter'].Value; $area = [math]::PI * ($dia / 2) * ($dia / 2)
    foreach ($p in @(@('Default flow rate', 'Default linear flow rate'), @('Max flow rate', 'Max linear flow rate'))) {
        if (-not $final[$p[0]].Value) { continue }
        $exp = [math]::Round([double]$final[$p[0]].Value / $area * 60, 2); $got = [double]$final[$p[1]].Value
        $ok = [math]::Abs($got - $exp) -lt 0.5; if (-not $ok) { $pass = $false }
        Write-Host ("    {0,-26} got={1,-8} expected~{2,-8} {3}" -f $p[1], $got, $exp, $(if ($ok) { 'OK (recomputed)' } else { 'MISMATCH' }))
    }
}
else {
    Info "column present: $($final['Name'].Value)"
    Info "diameter=$($final['Hardware diameter'].Value) bed height=$($final['Bed height'].Value) technique=$($final['Technique'].Value)"
}

Write-Host ""
if ($pass) { Write-Host "RESULT: SYNC OK" -ForegroundColor Green; $exit = 0 }
else { Write-Host "RESULT: SYNC FAILED (read-back mismatch)" -ForegroundColor Red; $exit = 1 }

} finally {
    try { Invoke-RestMethod -Method Delete -Uri "$Base/session/$sid" | Out-Null } catch {}
    # Safety: if we failed mid-Edit, don't leave the modal open (it would
    # block the main dialog and poison the next run). Esc cancels it,
    # discarding any partial change. Esc is more reliable here than clicking
    # the owner-drawn Cancel button.
    $stray = [Nav]::FindPre($u.Id, 'Edit Column type')
    if ($stray -ne [IntPtr]::Zero) {
        Write-Host "    (cleanup) closing stray Edit dialog" -ForegroundColor Yellow
        [void][Nav]::SetForegroundWindow($stray); Start-Sleep -Milliseconds 500
        [System.Windows.Forms.SendKeys]::SendWait('{ESC}'); Start-Sleep -Milliseconds 800
    }
}
if ($null -eq $exit) { $exit = 1 }
exit $exit
