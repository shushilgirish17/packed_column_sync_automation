<#
ensure_unicorn.ps1
------------------
Guarantee that UNICORN is running, logged in, a module is open, and the
Column Handling dialog is visible - so sync_column.ps1 can run.

Logic:
  * If UNICORN is already running with a visible window -> skip launch/login,
    just make sure Column Handling is open.
  * If not running -> launch it (Start-menu search, or UNICORN_EXE if set),
    enter the password from .env, open the module (default Evaluation), then
    open Column Handling.

Exit codes: 0 = Column Handling is open and ready, 1 = could not get there.

SECURITY: the UNICORN password is read on demand via Get-EnvValue
('UNICORN_PASSWORD') and typed with SendKeys. It is never written to the
console, a log, or the printable config object.

NOTE: the launch + login + module-select steps drive dialogs that can only be
seen when UNICORN is NOT already running, so their control selectors are
best-effort and marked TODO - confirm them on one run with the login dialog
visible. The "already running -> open Column Handling" path is verified.
#>

param(
    [int]$LoginTimeoutSec = 60,       # wait for the Login dialog after launch
    [int]$StartupWaitSec  = 120,      # wait for modules to load after LOG IN
    [int]$PostLaunchSettleSec = 15    # let a freshly launched UNICORN quiesce
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
. (Join-Path $root 'lib\env.ps1')
. (Join-Path $root 'lib\win32_dialogs.ps1')
$cfg = Get-ProjectConfig

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;using System.Text;using System.Collections.Generic;using System.Runtime.InteropServices;
public class Boot{public delegate bool EP(IntPtr h,IntPtr l);
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
public static List<IntPtr> Visible(int pid){var o=new List<IntPtr>();
 EnumWindows((h,l)=>{int p;GetWindowThreadProcessId(h,out p);
  if(p==pid&&IsWindowVisible(h)){var s=new StringBuilder(400);GetWindowTextW(h,s,400);if(s.Length>0)o.Add(h);}return true;},IntPtr.Zero);return o;}
public static IntPtr FindEqAnyPid(string title){IntPtr f=IntPtr.Zero;
 EnumWindows((h,l)=>{if(IsWindowVisible(h)){var s=new StringBuilder(400);GetWindowTextW(h,s,400);if(s.ToString()==title){f=h;return false;}}return true;},IntPtr.Zero);return f;}
public static string Text(IntPtr h){var s=new StringBuilder(400);GetWindowTextW(h,s,400);return s.ToString();}}
"@

$AE   = [System.Windows.Automation.AutomationElement]
$TS   = [System.Windows.Automation.TreeScope]
$COND = [System.Windows.Automation.Condition]

function Log($m) { Write-Host ("{0}  {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) }
function CtOf($el) { return ($el.Current.ControlType.ProgrammaticName -replace '^ControlType\.','') }

function MouseClick($hwnd, $rect) {
    # Foregrounds + verifies the target pixel is UNICORN's before clicking.
    return (Invoke-UnicornMouseClick -Window $hwnd -Rect $rect)
}

function Get-Unicorn { Get-Process $cfg.UnicornProcess -ErrorAction SilentlyContinue }

function ColumnHandling-Open {
    return ([Boot]::FindEqAnyPid($cfg.ColumnDialogTitle) -ne [IntPtr]::Zero)
}

function Open-ColumnHandling([int]$Attempts = 3) {
    # Click the 'Column Handling' button in any visible UNICORN module window.
    # Retried: the click is refused (not sent blind) when UNICORN can't be
    # brought in front of whatever window is covering it.
    for ($a = 1; $a -le $Attempts; $a++) {
        $u = Get-Unicorn
        if (-not $u) { return $false }
        $found = $false
        foreach ($h in [Boot]::Visible($u.Id)) {
            $win = $AE::FromHandle($h)
            foreach ($e in $win.FindAll($TS::Descendants, $COND::TrueCondition)) {
                if ((CtOf $e) -eq 'Button' -and $e.Current.Name -eq $cfg.ColumnDialogTitle -and $e.Current.IsEnabled) {
                    $found = $true
                    Log "clicking '$($cfg.ColumnDialogTitle)' button in '$([Boot]::Text($h))' (attempt $a/$Attempts)"
                    if (-not (MouseClick $h $e.Current.BoundingRectangle)) { continue }
                    for ($i = 0; $i -lt 20; $i++) { Start-Sleep -Milliseconds 500; if (ColumnHandling-Open) { return $true } }
                }
            }
        }
        if (-not $found) {
            # No specific 'Button' in the tree usually means the UIA provider has
            # degraded to generic Panes - only a UNICORN restart fixes that.
            Log "  no enabled '$($cfg.ColumnDialogTitle)' button found in any UNICORN window (UIA provider degraded? run tools\canary.ps1 / restart UNICORN)"
        }
        if (ColumnHandling-Open) { return $true }
        Start-Sleep -Seconds 1
    }
    return (ColumnHandling-Open)
}

function ClickXY($hwnd, $x, $y) {
    return (Invoke-UnicornMouseClick -Window $hwnd -Rect ([pscustomobject]@{ X = $x; Y = $y; Width = 0; Height = 0 }))
}

function Escape-SendKeys([string]$s) {
    # SendKeys treats + ^ % ~ ( ) { } [ ] specially; wrap each in {} to send
    # it literally, so any password types correctly. '{'+ch+'}' also yields the
    # correct {{} / {}} forms for the braces themselves.
    -join ($s.ToCharArray() | ForEach-Object {
        if ('+^%~(){}[]'.IndexOf($_) -ge 0) { '{' + $_ + '}' } else { "$_" }
    })
}

function Module-WindowPresent {
    # A visible UNICORN window other than the Login dialog == modules loaded.
    $u = Get-Unicorn
    if (-not $u) { return $false }
    foreach ($h in [Boot]::Visible($u.Id)) {
        $t = [Boot]::Text($h)
        if ($t -and $t -ne 'Login') { return $true }
    }
    return $false
}

function Do-Login {
    # Sign in on the 'Login' dialog. User Name / Access / modules are left at
    # their remembered defaults; only the password is entered. Never logs it.
    $h = [Boot]::FindEqAnyPid('Login')
    if ($h -eq [IntPtr]::Zero) { return $false }
    $win = $AE::FromHandle($h)
    $lblPw = $null; $cboUser = $null; $btnOK = $null
    foreach ($e in $win.FindAll($TS::Descendants, $COND::TrueCondition)) {
        switch ($e.Current.AutomationId) {
            'lblPassword' { $lblPw = $e }
            'btnOK'       { $btnOK = $e }
        }
        if (-not $cboUser -and (CtOf $e) -eq 'ComboBox' -and $e.Current.Name -eq 'User Name') { $cboUser = $e }
    }
    if (-not $lblPw -or -not $btnOK) { Log "  login controls not found"; return $false }

    $pw = Get-EnvValue 'UNICORN_PASSWORD'
    if (-not $pw) { Log "  UNICORN_PASSWORD not set in .env - aborting"; return $false }

    # Focus the password field: same row as the 'Password' label, same column
    # as the User Name box (the field itself isn't exposed to UIA).
    $lr = $lblPw.Current.BoundingRectangle
    $py = [int]($lr.Y + $lr.Height / 2)
    $px = if ($cboUser) {
        $cr = $cboUser.Current.BoundingRectangle; [int]($cr.X + $cr.Width / 2)
    } else { [int]($lr.X + $lr.Width + 120) }

    # The password goes out via SendKeys to whatever has focus - so refuse to
    # type it unless the Login dialog is verifiably in front and got the click.
    if (-not (ClickXY $h $px $py) -or -not (Set-UnicornForeground -Window $h)) {
        $pw = $null
        Log "  Login dialog could not be brought to the foreground - not typing the password"
        return $false
    }
    [System.Windows.Forms.SendKeys]::SendWait((Escape-SendKeys $pw))   # never logged
    $pw = $null
    Start-Sleep -Milliseconds 300
    Log "  password entered; clicking LOG IN"
    if (-not (MouseClick $h $btnOK.Current.BoundingRectangle)) { Log "  could not click LOG IN"; return $false }
    Start-Sleep -Seconds 2
    # Authentication + module load takes a while; readiness is decided by the
    # module-window wait in the caller, not here. Returning true just means the
    # credentials were submitted.
    return $true
}

function Ensure-WinAppDriver {
    # sync_column talks to WinAppDriver (port 4723). It is a SEPARATE server from
    # UNICORN and nothing else starts it, so make sure it is up here.
    $url = $cfg.WinAppDriverUrl
    try { Invoke-RestMethod -Uri "$url/status" -TimeoutSec 3 | Out-Null; Log "WinAppDriver already running"; return $true } catch {}

    $exe = $cfg.WinAppDriverExe
    if (-not $exe -or -not (Test-Path $exe)) {
        $exe = @(
            'C:\Program Files (x86)\Windows Application Driver\WinAppDriver.exe',
            'C:\Program Files\Windows Application Driver\WinAppDriver.exe'
        ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    }
    if (-not $exe) { Log "WinAppDriver.exe not found - set WINAPPDRIVER_EXE in .env"; return $false }

    Log "WinAppDriver not running - starting $exe"
    Start-Process -FilePath $exe | Out-Null
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 750
        try { Invoke-RestMethod -Uri "$url/status" -TimeoutSec 3 | Out-Null; Log "WinAppDriver ready"; return $true } catch {}
    }
    Log "WinAppDriver did not become reachable at $url"
    return $false
}

# ---------------------------------------------------------------------------
# 0. Make sure WinAppDriver (the automation server) is up.
# ---------------------------------------------------------------------------
if (-not (Ensure-WinAppDriver)) { Log "cannot proceed without WinAppDriver"; exit 1 }

# ---------------------------------------------------------------------------
# 1. Get UNICORN to a logged-in state with a module window open.
#    States handled: already logged in / sitting at Login / not running.
# ---------------------------------------------------------------------------
$login = [Boot]::FindEqAnyPid('Login')

if ((Module-WindowPresent) -and $login -eq [IntPtr]::Zero) {
    $u = Get-Unicorn
    Log "UNICORN already running and logged in (pid $($u.Id)) - skipping launch/login"
}
else {
    if ($login -eq [IntPtr]::Zero -and -not (Get-Unicorn)) {
        Log "UNICORN not running - launching"
        if ($cfg.UnicornExe -and (Test-Path $cfg.UnicornExe)) {
            Log "  starting $($cfg.UnicornExe)"
            Start-Process -FilePath $cfg.UnicornExe | Out-Null
        }
        else {
            Log "  launching via Start-menu search"
            $wsh = New-Object -ComObject WScript.Shell
            $wsh.SendKeys('^{ESC}'); Start-Sleep -Milliseconds 800
            $wsh.SendKeys('UNICORN'); Start-Sleep -Milliseconds 1200
            $wsh.SendKeys('{ENTER}')
        }
        $deadline = [Environment]::TickCount + ($LoginTimeoutSec * 1000)
        while ([Environment]::TickCount -lt $deadline -and $login -eq [IntPtr]::Zero) {
            Start-Sleep -Milliseconds 700
            $login = [Boot]::FindEqAnyPid('Login')
        }
        if ($login -eq [IntPtr]::Zero) { Log "  Login dialog not seen in ${LoginTimeoutSec}s - aborting"; exit 1 }
    }
    else {
        Log "UNICORN is at the Login dialog"
    }

    Log "signing in (User Name / Access / modules left at defaults)..."
    if (-not (Do-Login)) { Log "login failed"; exit 1 }

    # UNICORN takes a while to open the selected modules after LOG IN.
    Log "logged in - waiting up to ${StartupWaitSec}s for UNICORN to open..."
    $deadline = [Environment]::TickCount + ($StartupWaitSec * 1000)
    while (-not (Module-WindowPresent) -and [Environment]::TickCount -lt $deadline) {
        Start-Sleep -Seconds 2
    }
    if (-not (Module-WindowPresent)) { Log "UNICORN did not open a module window in ${StartupWaitSec}s - aborting"; exit 1 }
    Log "UNICORN modules loaded"

    # A just-launched UNICORN is still wiring up its DB connection and dialogs;
    # firing an import immediately races that and can drop keystrokes / reject
    # the file. Give a freshly launched instance a moment to quiesce. (Only on a
    # cold launch - the already-running path above skips this.)
    Log "letting a freshly launched UNICORN settle for ${PostLaunchSettleSec}s..."
    Start-Sleep -Seconds $PostLaunchSettleSec
}

# ---------------------------------------------------------------------------
# 2. Ensure Column Handling is open
# ---------------------------------------------------------------------------
if (ColumnHandling-Open) {
    Log "Column Handling already open - ready"
    exit 0
}
Log "opening Column Handling..."
if (Open-ColumnHandling) { Log "Column Handling open - ready"; exit 0 }
Log "could not open Column Handling"
exit 1
