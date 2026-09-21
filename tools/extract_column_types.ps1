<#
extract_column_types.ps1
------------------------
Extract UNICORN's Column Type library to CSV, fast.

Hybrid by necessity -- each half does the thing only it can do:

  CLICK  via WinAppDriver (REST). A real click is required: UIA's
         SelectionItemPattern.Select() moves the list selection but does NOT
         fire the WinForms SelectedIndexChanged handler that repopulates the
         parameter grids. Using Select() silently produced a CSV in which
         every row repeated the first column type's values.

  READ   via in-process .NET UI Automation. WinAppDriver's HTTP costs ~230 ms
         per call and a full read is ~55 calls per column type (~14 s each,
         ~2 h for 555). Local COM reads are an order of magnitude faster.

Also note the list is virtualised in the .NET UIA view (~46 items realised) but
fully enumerable through WinAppDriver -- another reason for the split.

Read-only with respect to data: only the list selection changes. No command
button is invoked, no modal opened, nothing written back to UNICORN.

Prerequisites: UNICORN open with Column Handling visible; WinAppDriver running.

Usage
  powershell -NoProfile -ExecutionPolicy Bypass -File extract_column_types.ps1 -Limit 25
  powershell -NoProfile -ExecutionPolicy Bypass -File extract_column_types.ps1
#>

param(
    [int]$Limit = 0,
    [string]$OutDir,          # defaults to config EXPORTS_DIR
    [int]$SettleMs = 500,
    [string]$Base             # defaults to config WINAPPDRIVER_URL
)

# Load shared config (.env-backed). CLI params override it when supplied.
. (Join-Path (Split-Path $PSScriptRoot -Parent) 'lib\env.ps1')
$cfg = Get-ProjectConfig
if (-not $Base)   { $Base   = $cfg.WinAppDriverUrl }
if (-not $OutDir) { $OutDir = $cfg.ExportsDir }
$Proc  = $cfg.UnicornProcess
$Title = $cfg.ColumnDialogTitle

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @"
using System;using System.Text;using System.Runtime.InteropServices;
public class W{public delegate bool EP(IntPtr h,IntPtr l);
[DllImport("user32.dll")]public static extern bool EnumWindows(EP cb,IntPtr l);
[DllImport("user32.dll",CharSet=CharSet.Unicode)]public static extern int GetWindowTextW(IntPtr h,StringBuilder s,int n);
[DllImport("user32.dll")]public static extern int GetWindowThreadProcessId(IntPtr h,out int pid);
[DllImport("user32.dll")]public static extern bool IsWindowVisible(IntPtr h);
[DllImport("user32.dll")]public static extern IntPtr GetWindow(IntPtr h,uint c);
public static IntPtr Find(int pid,string title){IntPtr f=IntPtr.Zero;
 EnumWindows((h,l)=>{int p;GetWindowThreadProcessId(h,out p);
  if(p==pid&&IsWindowVisible(h)){var sb=new StringBuilder(512);GetWindowTextW(h,sb,512);
   if(sb.ToString()==title){f=h;return false;}}return true;},IntPtr.Zero);return f;}}
"@

$AE   = [System.Windows.Automation.AutomationElement]
$TS   = [System.Windows.Automation.TreeScope]
$COND = [System.Windows.Automation.Condition]
$VPAT = [System.Windows.Automation.ValuePattern]::Pattern
$GW_OWNER = 4

function CtOf($el) { return ($el.Current.ControlType.ProgrammaticName -replace '^ControlType\.','') }

function CellText($el) {
    # Values live in ValuePattern; the accessible Name is positional
    # ("Value Row 3"). WinForms renders an empty cell as the literal "(null)".
    foreach ($p in $el.GetSupportedPatterns()) {
        if ($p.ProgrammaticName -like 'Value*') {
            $v = $el.GetCurrentPattern($VPAT).Current.Value
            if ($v -eq '(null)') { return '' }
            if ($v) { return $v.Trim() }
        }
    }
    return ''
}

# --- locate the dialog and its owning top-level window --------------------
$u = Get-Process $Proc -ErrorAction SilentlyContinue
if (-not $u) { "$Proc is not running."; exit 2 }
$dlgH = [W]::Find($u.Id, $Title)
if ($dlgH -eq [IntPtr]::Zero) { "The $Title dialog is not open."; exit 2 }
$owner = [W]::GetWindow($dlgH, $GW_OWNER)
"dialog=0x$('{0:x}' -f [int64]$dlgH)  owner=0x$('{0:x}' -f [int64]$owner)"

$dlg = $AE::FromHandle($dlgH)

# --- WinAppDriver session (JSONWP: unprefixed desiredCapabilities) --------
function WadPost($path, $body) {
    Invoke-RestMethod -Method Post -Uri "$Base$path" -ContentType 'application/json' `
        -Body ($body | ConvertTo-Json -Depth 6 -Compress)
}
try {
    $sess = WadPost '/session' @{ desiredCapabilities = @{
        platformName = 'Windows'; appTopLevelWindow = ('0x{0:x}' -f [int64]$owner) } }
} catch { "Could not start a WinAppDriver session: $($_.Exception.Message)"; exit 2 }
$sid = $sess.sessionId
"WinAppDriver session $sid"

try {
    # Ensure the Column Type Parameters tab is active.
    try {
        $t = WadPost "/session/$sid/element" @{ using='xpath'; value="//TabItem[@Name='Column Type Parameters']" }
        WadPost "/session/$sid/element/$($t.value.ELEMENT)/click" @{} | Out-Null
        Start-Sleep -Milliseconds 1200
    } catch { }

    $res = WadPost "/session/$sid/elements" @{ using='xpath'
        value = "//List[starts-with(@Name,'Column types')]//ListItem" }
    $ids = @($res.value | ForEach-Object { $_.ELEMENT })
    "column types listed: $($ids.Count)"
    if ($Limit -gt 0 -and $Limit -lt $ids.Count) { $ids = $ids[0..($Limit-1)] }
    "extracting: $($ids.Count)"

    $records = @()
    $sw = [Diagnostics.Stopwatch]::StartNew()

    foreach ($eid in $ids) {
        try { WadPost "/session/$sid/element/$eid/click" @{} | Out-Null }
        catch { Write-Host "  ! click failed: $($_.Exception.Message)"; continue }
        Start-Sleep -Milliseconds $SettleMs

        # Read all three grids locally. Rows are Custom elements named
        # 'Row N'; cells are DataItem children in Parameters/Value/Unit order.
        $row = [ordered]@{ ColumnType = '' }
        foreach ($g in $dlg.FindAll($TS::Descendants, $COND::TrueCondition)) {
            if ((CtOf $g) -ne 'DataGrid') { continue }
            foreach ($r in $g.FindAll($TS::Children, $COND::TrueCondition)) {
                if ($r.Current.Name -eq 'Top Row') { continue }
                $cells = @()
                foreach ($c in $r.FindAll($TS::Children, $COND::TrueCondition)) {
                    if ((CtOf $c) -eq 'DataItem') { $cells += (CellText $c) }
                }
                if ($cells.Count -lt 2 -or -not $cells[0]) { continue }
                $param = $cells[0]
                if ($row.Contains($param)) { continue }
                $row[$param] = $cells[1]
                if ($cells.Count -gt 2 -and $cells[2]) { $row["$param (unit)"] = $cells[2] }
            }
        }
        # The Ordering Information grid carries the column type's own name,
        # so we get it without another WinAppDriver round trip.
        if ($row.Contains('Name')) { $row['ColumnType'] = $row['Name'] }
        $records += $row

        $rate = $sw.Elapsed.TotalSeconds / $records.Count
        Write-Host ("  [{0}/{1}] {2}   ({3:N1}s/item)" -f $records.Count, $ids.Count, $row['ColumnType'], $rate)
    }
} finally {
    try { Invoke-RestMethod -Method Delete -Uri "$Base/session/$sid" | Out-Null } catch { }
}

if ($records.Count -eq 0) { "Nothing extracted."; exit 1 }

# --- write CSV -------------------------------------------------------------
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir | Out-Null }
$csv = Join-Path $OutDir ("column_types_" + (Get-Date -Format 'yyyyMMdd-HHmmss') + ".csv")

$keys = [System.Collections.Specialized.OrderedDictionary]::new()
foreach ($r in $records) { foreach ($k in $r.Keys) { $keys[$k] = $true } }
$useful = @(foreach ($k in @($keys.Keys)) {
    $any = $false
    foreach ($r in $records) { if ($r.Contains($k) -and "$($r[$k])".Trim() -ne '') { $any = $true; break } }
    if ($any) { $k }
})

$normalised = foreach ($r in $records) {
    $o = [ordered]@{}
    foreach ($k in $useful) { $o[$k] = if ($r.Contains($k)) { $r[$k] } else { '' } }
    [pscustomobject]$o
}
$normalised | Export-Csv -Path $csv -NoTypeInformation -Encoding UTF8

""
"Extracted $($records.Count) column types, $($useful.Count) populated fields."
"Written to: $csv"
