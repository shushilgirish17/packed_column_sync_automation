<#
canary.ps1
----------
Health check for UNICORN's UIA provider. Run this BEFORE any automation run.

Why it exists
=============
Certain UIA calls (notably InvokePattern.Invoke on a control that opens a
modal dialog) permanently kill UNICORN's managed WinForms UIA provider. When
that happens the app keeps working normally for a human -- Responding=True,
no error, nothing visible -- but automation silently degrades:

  healthy   Button 'btnNewColumn',  List 'lbxColumnTypes',  ~1.2 s for 719 elements
  degraded  everything is a nameless Pane with a numeric id, ~3 s PER CALL

Only a restart of UNICORNClient clears it. So a runner must detect this up
front rather than crawling for 40 s and drawing wrong conclusions.

READ-ONLY. Reads control types and names; invokes nothing.

Exit codes: 0 = healthy, 1 = degraded, 2 = dialog/app not found.
#>

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @"
using System; using System.Text; using System.Collections.Generic; using System.Runtime.InteropServices;
public class Cw {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern int GetWindowThreadProcessId(IntPtr h, out int pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  // Win32 keeps working even when UIA is wedged -- use it to find the window.
  public static IntPtr Find(int pid, string title) {
    IntPtr found = IntPtr.Zero;
    EnumWindows((h,l) => {
      int p; GetWindowThreadProcessId(h, out p);
      if (p == pid && IsWindowVisible(h)) {
        var sb = new StringBuilder(256); GetWindowTextW(h, sb, 256);
        if (sb.ToString() == title) { found = h; return false; }
      }
      return true;
    }, IntPtr.Zero);
    return found;
  }
}
"@

$AE = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]
$COND = [System.Windows.Automation.Condition]

# Shared config (.env-backed).
. (Join-Path (Split-Path $PSScriptRoot -Parent) 'lib\env.ps1')
$cfg = Get-ProjectConfig

$u = Get-Process $cfg.UnicornProcess -ErrorAction SilentlyContinue
if (-not $u) { "$($cfg.UnicornProcess) is not running."; exit 2 }
"$($cfg.UnicornProcess) pid=$($u.Id) started $($u.StartTime)"

$hwnd = [Cw]::Find($u.Id, $cfg.ColumnDialogTitle)
if ($hwnd -eq [IntPtr]::Zero) { "$($cfg.ColumnDialogTitle) dialog is not open."; exit 2 }
"Dialog hwnd = 0x{0:x} (located via Win32, which survives a wedged provider)" -f [int64]$hwnd
""

# --- cheap canary: direct children only -----------------------------------
# A healthy provider exposes typed controls here (Button/List/Tab). A dead
# one collapses everything to generic Pane.
$el = $AE::FromHandle($hwnd)
$sw = [Diagnostics.Stopwatch]::StartNew()
$kids = $el.FindAll($TS::Children, $COND::TrueCondition)
$kidMs = $sw.ElapsedMilliseconds

$types = @{}
foreach ($k in $kids) {
    $t = ($k.Current.ControlType.ProgrammaticName -replace '^ControlType\.','')
    $types[$t] = 1 + ($(if ($types.ContainsKey($t)) { $types[$t] } else { 0 }))
}
"Direct children: $($kids.Count) in $kidMs ms"
"  types: " + (($types.GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join '  ')

$typed = 0
foreach ($t in @('Button','List','Tab','ComboBox','DataGrid','TitleBar','Text')) {
    if ($types.ContainsKey($t)) { $typed += $types[$t] }
}

if ($typed -eq 0 -or $kidMs -gt 5000) {
    ""
    "VERDICT: DEGRADED - the managed UIA provider is dead."
    "         Every child is a generic Pane and/or calls are hitting the ~3 s"
    "         fallback timeout. Restart UNICORNClient before automating."
    exit 1
}

# --- full read, only worth doing if the cheap check passed ----------------
$sw2 = [Diagnostics.Stopwatch]::StartNew()
$all = $el.FindAll($TS::Descendants, $COND::TrueCondition)
$allMs = $sw2.ElapsedMilliseconds
"Full subtree: $($all.Count) elements in $allMs ms"

# --- named-control spot check ---------------------------------------------
# Dialog-level controls: always present regardless of which tab is active.
$always = @('btnClose','tabColumnType','tabColumnLogbook')
# Tab-scoped controls: a TabPage's children are only present in the UIA tree
# WHILE that tab is the selected one -- they disappear again when you switch
# away (verified: activating a tab then switching back removes them). So
# absence here is normal, not a provider fault. Select the tab, then look.
$tabScoped = @{
    'btnNewColumn'    = 'Column Logbook'
    'btnImportColumn' = 'Column Logbook'
    'btnNewType'      = 'Column Type Parameters'
    'btnImportType'   = 'Column Type Parameters'
}

$seen = @{}
foreach ($e in $all) {
    $aid = $e.Current.AutomationId
    if ($always -contains $aid -or $tabScoped.ContainsKey($aid)) {
        $seen[$aid] = ($e.Current.ControlType.ProgrammaticName -replace '^ControlType\.','')
    }
}

""
"Dialog-level controls (must always be present):"
foreach ($w in $always) {
    if ($seen.ContainsKey($w)) { "  OK      $w -> $($seen[$w])" } else { "  MISSING $w" }
}
""
"Tab-scoped controls (present only while their tab is selected -- not a fault):"
foreach ($w in ($tabScoped.Keys | Sort-Object)) {
    if ($seen.ContainsKey($w)) { "  in tree      $w -> $($seen[$w])" }
    else { "  not in tree  $w  (select '$($tabScoped[$w])' to address it)" }
}

$missingAlways = @($always | Where-Object { -not $seen.ContainsKey($_) }).Count

""
if ($missingAlways -eq 0 -and $allMs -lt 15000) {
    "VERDICT: HEALTHY - rich WinForms provider is present and responsive."
    exit 0
} else {
    "VERDICT: DEGRADED - dialog-level controls missing or read too slow."
    exit 1
}
