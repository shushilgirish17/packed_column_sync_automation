<#
win32_dialogs.ps1
-----------------
Helpers for driving UNICORN's message boxes and confirmation dialogs without
synthetic mouse input.

Why this exists
===============
UNICORN's message boxes ("Import" errors, "Confirm Column Type Delete", ...)
do NOT expose their buttons to UI Automation -- walking the dialog's UIA
descendants finds no Button elements, and AutomationElement.FromHandle on the
button HWND throws "Unsupported Pattern". So UIA cannot press them at all.

Synthetic mouse works but steals focus and moves the physical cursor, which is
unacceptable for an unattended runner.

Measured against a WinForms message box, four message-based methods work and
one does not:

    SendMessage(btn, BM_CLICK)                        WORKS
    SendMessage(dlg, WM_COMMAND, id|BN_CLICKED, btn)  WORKS
    PostMessage(dlg, WM_COMMAND, id|BN_CLICKED, btn)  WORKS
    PostMessage(btn, WM_LBUTTONDOWN + WM_LBUTTONUP)   WORKS
    PostMessage(btn, BM_CLICK)                        DOES NOT  <-- the trap

The last one is the one to avoid: posting BM_CLICK is silently ignored, which
is what made an earlier attempt look like the dialog was unresponsive.
Confirmed on a real UNICORN "Import" error dialog: SendMessage + BM_CLICK
closes it, no focus change, no cursor movement.

SendMessage is synchronous, so it is wrapped in SendMessageTimeout with
SMTO_ABORTIFHUNG here -- otherwise a busy UNICORN would hang the caller.

Usage
-----
    . .\win32_dialogs.ps1

    $dlg = Find-UnicornDialog -TitleEquals 'Confirm Column Type Delete'
    Get-DialogButtons $dlg                      # inspect before acting
    Invoke-DialogButton -Dialog $dlg -Text 'No'
#>

Add-Type @"
using System;using System.Text;using System.Collections.Generic;using System.Runtime.InteropServices;
public class W32Dlg{
  public delegate bool EP(IntPtr h,IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EP cb,IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr p,EP cb,IntPtr l);
  [DllImport("user32.dll",CharSet=CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h,StringBuilder s,int n);
  [DllImport("user32.dll",CharSet=CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h,StringBuilder s,int n);
  [DllImport("user32.dll")] public static extern int GetWindowThreadProcessId(IntPtr h,out int pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsWindowEnabled(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern int GetDlgCtrlID(IntPtr h);
  [DllImport("user32.dll",CharSet=CharSet.Auto)]
  public static extern IntPtr SendMessageTimeout(IntPtr h,uint m,IntPtr w,IntPtr l,uint flags,uint ms,out IntPtr res);

  public static List<IntPtr> TopLevel(int pid){
    var o=new List<IntPtr>();
    EnumWindows((h,l)=>{ int p; GetWindowThreadProcessId(h,out p);
      if(p==pid && IsWindowVisible(h)){ var sb=new StringBuilder(512); GetWindowTextW(h,sb,512);
        if(sb.Length>0) o.Add(h); }
      return true; }, IntPtr.Zero);
    return o; }
  public static List<IntPtr> Buttons(IntPtr h){
    var o=new List<IntPtr>();
    EnumChildWindows(h,(c,l)=>{ var cn=new StringBuilder(64); GetClassNameW(c,cn,64);
      if(cn.ToString()=="Button") o.Add(c); return true; }, IntPtr.Zero);
    return o; }
  public static List<string> Texts(IntPtr h){
    var o=new List<string>();
    EnumChildWindows(h,(c,l)=>{ if(IsWindowVisible(c)){ var sb=new StringBuilder(512); GetWindowTextW(c,sb,512);
      if(sb.Length>0) o.Add(sb.ToString()); } return true; }, IntPtr.Zero);
    return o; }
  public static string Text(IntPtr h){ var sb=new StringBuilder(512); GetWindowTextW(h,sb,512); return sb.ToString(); }
  public static int Id(IntPtr h){ return GetDlgCtrlID(h); }
}
"@

Add-Type @"
using System;using System.Runtime.InteropServices;
public class W32Focus{
  public struct POINT{public int X;public int Y;}
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h,int cmd);
  [DllImport("user32.dll")] public static extern IntPtr GetAncestor(IntPtr h,uint f);
  [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr h,uint cmd);
  [DllImport("user32.dll")] public static extern IntPtr WindowFromPoint(POINT p);
  [DllImport("user32.dll")] public static extern int GetWindowThreadProcessId(IntPtr h,out int pid);
  [DllImport("user32.dll")] public static extern int GetWindowThreadProcessId(IntPtr h,IntPtr pid);
  [DllImport("kernel32.dll")] public static extern int GetCurrentThreadId();
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(int a,int b,bool attach);
  [DllImport("user32.dll")] public static extern void keybd_event(byte vk,byte scan,uint f,UIntPtr e);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x,int y);
  [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT p);
  [DllImport("user32.dll")] public static extern void mouse_event(uint f,int dx,int dy,uint d,UIntPtr e);
  public static int PidOf(IntPtr h){ int p; GetWindowThreadProcessId(h,out p); return p; }
  public static IntPtr Root(IntPtr h){ return GetAncestor(h,2); }
  public static IntPtr At(int x,int y){ var p=new POINT(); p.X=x; p.Y=y; return WindowFromPoint(p); }
}
"@

function Set-UnicornForeground {
    <#
      Bring a UNICORN window to the foreground and CONFIRM it got there.

      A bare SetForegroundWindow is refused by Windows' foreground lock whenever
      the caller is not the foreground process - which is always the case for
      watch_service's child scripts. It fails silently, and a synthetic click
      then lands on whatever app is covering UNICORN (seen: the button centre was
      under another window, the click went there, Column Handling never opened).
      Escalate: plain call -> AttachThreadInput to the current foreground thread
      -> an ALT tap (makes this process the last-input owner, which lifts the
      lock). Never minimizes (that closes UNICORN's modal dialogs).
      Returns $true only if the window is verifiably in front.
    #>
    param([Parameter(Mandatory)][IntPtr]$Window, [int]$Attempts = 3)
    if ([W32Focus]::IsIconic($Window)) { [void][W32Focus]::ShowWindow($Window, 9) }   # SW_RESTORE
    $isFront = { [W32Focus]::Root([W32Focus]::GetForegroundWindow()) -eq [W32Focus]::Root($Window) }
    for ($i = 1; $i -le $Attempts; $i++) {
        [void][W32Focus]::SetForegroundWindow($Window); Start-Sleep -Milliseconds 150
        if (& $isFront) { return $true }

        $fgThread = [W32Focus]::GetWindowThreadProcessId([W32Focus]::GetForegroundWindow(), [IntPtr]::Zero)
        $me = [W32Focus]::GetCurrentThreadId()
        if ($fgThread -and $fgThread -ne $me -and [W32Focus]::AttachThreadInput($me, $fgThread, $true)) {
            try {
                [void][W32Focus]::BringWindowToTop($Window)
                [void][W32Focus]::SetForegroundWindow($Window)
            } finally { [void][W32Focus]::AttachThreadInput($me, $fgThread, $false) }
            Start-Sleep -Milliseconds 150
            if (& $isFront) { return $true }
        }

        [W32Focus]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero)          # ALT down
        [W32Focus]::keybd_event(0x12, 0, 0x0002, [UIntPtr]::Zero)     # ALT up
        [void][W32Focus]::SetForegroundWindow($Window)
        Start-Sleep -Milliseconds 250
        if (& $isFront) { return $true }
    }
    return $false
}

function Invoke-UnicornMouseClick {
    <#
      Synthetic click at the centre of an element's screen rect (works at
      negative coords / owner-drawn buttons, unlike WinAppDriver's click).
      Foregrounds $Window first and then checks that the pixel under the target
      really belongs to UNICORN - if another window still covers it, it does NOT
      click (clicking blind into another app is how jobs failed before, and is
      unsafe). Restores the cursor. Returns $true if the click was delivered.
    #>
    param([Parameter(Mandatory)][IntPtr]$Window, [Parameter(Mandatory)]$Rect)
    $cx = [int]($Rect.X + $Rect.Width / 2); $cy = [int]($Rect.Y + $Rect.Height / 2)
    $pid_ = [W32Focus]::PidOf($Window)
    if (-not (Set-UnicornForeground -Window $Window)) {
        Write-Host "    ! could not bring '$([W32Dlg]::Text($Window))' to the foreground - click skipped"
        return $false
    }
    Start-Sleep -Milliseconds 250
    $hit = [W32Focus]::Root([W32Focus]::At($cx, $cy))
    if ([W32Focus]::PidOf($hit) -ne $pid_) {
        Write-Host "    ! target ($cx,$cy) is covered by '$([W32Dlg]::Text($hit))' (pid $([W32Focus]::PidOf($hit))) - click skipped"
        return $false
    }
    $o = New-Object W32Focus+POINT; [void][W32Focus]::GetCursorPos([ref]$o)
    [void][W32Focus]::SetCursorPos($cx, $cy); Start-Sleep -Milliseconds 120
    [W32Focus]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero); Start-Sleep -Milliseconds 60
    [W32Focus]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero); Start-Sleep -Milliseconds 200
    [void][W32Focus]::SetCursorPos($o.X, $o.Y)
    return $true
}

function Get-UnicornStrayDialogs {
    <#
      Modal windows left open over UNICORN (e.g. a 'New Column Type' or 'Edit Column type'
      someone opened, a file browser, a message box). Rule: a visible top-level UNICORN window
        - whose OWNER is disabled (it is modal and blocking something),
        - whose title is not in -Keep, and
        - whose owner chain runs through Column Handling or ends at one of -Modules.
      The last rule keeps us away from System Control: its prompts belong to a running
      instrument, and cancelling one could disturb a run. Module windows have no owner and
      floating tool windows don't disable theirs, so neither is ever touched.
      Topmost (still enabled) dialogs come first.
    #>
    param([string[]]$Keep = @('Column Handling'), [string[]]$Modules = @('Evaluation Classic', 'Evaluation'),
          [string]$ProcessName = 'UNICORNClient')
    $p = Get-Process $ProcessName -ErrorAction SilentlyContinue
    if (-not $p) { return @() }
    $out = foreach ($h in [W32Dlg]::TopLevel($p.Id)) {
        $title = [W32Dlg]::Text($h)
        if ($Keep -contains $title) { continue }
        $owner = [W32Focus]::GetWindow($h, 4)   # GW_OWNER
        if ($owner -eq [IntPtr]::Zero -or [W32Dlg]::IsWindowEnabled($owner)) { continue }
        $chain = @(); $o = $owner
        while ($o -ne [IntPtr]::Zero -and $chain.Count -lt 10) { $chain += [W32Dlg]::Text($o); $o = [W32Focus]::GetWindow($o, 4) }
        $inScope = ($chain | Where-Object { $Keep -contains $_ }) -or ($Modules -contains $chain[-1])
        if (-not $inScope) { continue }
        [pscustomobject]@{ Handle = $h; Title = $title; Topmost = [W32Dlg]::IsWindowEnabled($h); Owner = $chain[0] }
    }
    return @($out | Sort-Object { -not $_.Topmost })
}

function Clear-UnicornStrayDialogs {
    <#
      Close every stray modal (see Get-UnicornStrayDialogs) WITHOUT saving anything, so the
      automation can always get back to a clean Column Handling dialog. Per dialog, cheapest
      safe method first:
        1. a Win32 button No / Cancel / Close / Abort (never OK / Yes / Save). 'No' first so a
           "Save changes?" prompt discards rather than cancelling the close.
        2. WM_CLOSE - WinForms treats it as Cancel (UNICORN's owner-drawn dialogs).
        3. Esc to the foregrounded dialog.
      Repeats, because closing one dialog can reveal (or trigger) another.
      Returns the titles it closed; throws if something is still blocking afterwards.
    #>
    param([string[]]$Keep = @('Column Handling'), [string]$ProcessName = 'UNICORNClient',
          [int]$MaxRounds = 8, [switch]$WhatIf)
    $closed = @()
    for ($round = 1; $round -le $MaxRounds; $round++) {
        $strays = @(Get-UnicornStrayDialogs -Keep $Keep -ProcessName $ProcessName)
        if ($strays.Count -eq 0) { return $closed }
        if ($WhatIf) { return @($strays.Title) }
        $d = $strays[0]
        $res = [IntPtr]::Zero
        $btn = Get-DialogButtons -Dialog $d.Handle | Where-Object { $_.Enabled -and @('No', 'Cancel', 'Close', 'Abort') -contains $_.Text } |
               Sort-Object { @('No', 'Cancel', 'Close', 'Abort').IndexOf($_.Text) } | Select-Object -First 1
        if ($btn) { [void](Invoke-DialogButton -Dialog $d.Handle -Text $btn.Text -TimeoutMs 3000) }
        if ([W32Dlg]::IsWindow($d.Handle)) {
            [void][W32Dlg]::SendMessageTimeout($d.Handle, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero,   # WM_CLOSE
                                               $script:SMTO_ABORTIFHUNG, 3000, [ref]$res)
            for ($i = 0; $i -lt 10 -and [W32Dlg]::IsWindow($d.Handle); $i++) { Start-Sleep -Milliseconds 200 }
        }
        if ([W32Dlg]::IsWindow($d.Handle) -and (Set-UnicornForeground -Window $d.Handle)) {
            Add-Type -AssemblyName System.Windows.Forms
            [System.Windows.Forms.SendKeys]::SendWait('{ESC}')
            for ($i = 0; $i -lt 10 -and [W32Dlg]::IsWindow($d.Handle); $i++) { Start-Sleep -Milliseconds 200 }
        }
        if (-not [W32Dlg]::IsWindow($d.Handle)) { $closed += $d.Title }
        Start-Sleep -Milliseconds 400   # let a follow-up prompt (e.g. "Save changes?") appear
    }
    $left = @(Get-UnicornStrayDialogs -Keep $Keep -ProcessName $ProcessName)
    if ($left.Count) { throw "Could not close UNICORN dialog(s): $(($left.Title) -join ', ') - close manually." }
    return $closed
}

$script:BM_CLICK         = 0x00F5
$script:WM_COMMAND       = 0x0111
$script:BN_CLICKED       = 0
$script:SMTO_ABORTIFHUNG = 0x0002

function Find-UnicornDialog {
    <#
      Locate a visible top-level UNICORN window by exact title or prefix.
      Returns an IntPtr, or [IntPtr]::Zero if not found.
    #>
    param(
        [string]$TitleEquals,
        [string]$TitleStartsWith,
        [string]$ProcessName = 'UNICORNClient'
    )
    $p = Get-Process $ProcessName -ErrorAction SilentlyContinue
    if (-not $p) { return [IntPtr]::Zero }
    foreach ($h in [W32Dlg]::TopLevel($p.Id)) {
        $t = [W32Dlg]::Text($h)
        if ($TitleEquals -and $t -eq $TitleEquals) { return $h }
        if ($TitleStartsWith -and $t.StartsWith($TitleStartsWith)) { return $h }
    }
    return [IntPtr]::Zero
}

function Get-DialogText {
    param([Parameter(Mandatory)][IntPtr]$Dialog)
    return [W32Dlg]::Texts($Dialog)
}

function Get-DialogButtons {
    <# List a dialog's buttons: handle, control id, caption (ampersand stripped). #>
    param([Parameter(Mandatory)][IntPtr]$Dialog)
    foreach ($b in [W32Dlg]::Buttons($Dialog)) {
        [pscustomobject]@{
            Handle  = $b
            Id      = [W32Dlg]::Id($b)
            Text    = ([W32Dlg]::Text($b)) -replace '&',''
            Enabled = [W32Dlg]::IsWindowEnabled($b)
        }
    }
}

function Invoke-DialogButton {
    <#
      Press a dialog button by caption, via SendMessageTimeout + BM_CLICK.
      No focus change, no cursor movement.

      Returns $true if the dialog closed within -TimeoutMs.
    #>
    param(
        [Parameter(Mandatory)][IntPtr]$Dialog,
        [Parameter(Mandatory)][string]$Text,
        [int]$TimeoutMs = 5000
    )
    $btn = Get-DialogButtons -Dialog $Dialog | Where-Object { $_.Text -eq $Text } | Select-Object -First 1
    if (-not $btn) {
        Write-Error "No button captioned '$Text'. Present: $((Get-DialogButtons -Dialog $Dialog).Text -join ', ')"
        return $false
    }
    $res = [IntPtr]::Zero
    # Two message-based presses, both confirmed to work on a real UNICORN box
    # (see header). Try BM_CLICK on the button first, then, if it is still open,
    # WM_COMMAND(BN_CLICKED, id) to the dialog - a redundant path in case the
    # first is ignored. Neither steals focus or moves the cursor.
    $click = {
        [void][W32Dlg]::SendMessageTimeout($btn.Handle, $script:BM_CLICK, [IntPtr]::Zero, [IntPtr]::Zero,
                                           $script:SMTO_ABORTIFHUNG, 3000, [ref]$res)
    }
    $command = {
        $wparam = [IntPtr](($script:BN_CLICKED -shl 16) -bor ($btn.Id -band 0xFFFF))
        [void][W32Dlg]::SendMessageTimeout($Dialog, $script:WM_COMMAND, $wparam, $btn.Handle,
                                           $script:SMTO_ABORTIFHUNG, 3000, [ref]$res)
    }
    & $click
    $deadline = [Environment]::TickCount + $TimeoutMs
    $usedCommand = $false
    while ([Environment]::TickCount -lt $deadline) {
        if (-not [W32Dlg]::IsWindow($Dialog)) { return $true }
        if (-not $usedCommand -and [Environment]::TickCount -gt ($deadline - $TimeoutMs + 1500)) {
            & $command; $usedCommand = $true
        }
        Start-Sleep -Milliseconds 150
    }
    return (-not [W32Dlg]::IsWindow($Dialog))
}

function Wait-UnicornDialog {
    <# Wait for a dialog to appear; returns its handle or [IntPtr]::Zero. #>
    param(
        [string]$TitleEquals,
        [string]$TitleStartsWith,
        [int]$TimeoutMs = 15000
    )
    $deadline = [Environment]::TickCount + $TimeoutMs
    while ([Environment]::TickCount -lt $deadline) {
        $h = Find-UnicornDialog -TitleEquals $TitleEquals -TitleStartsWith $TitleStartsWith
        if ($h -ne [IntPtr]::Zero) { return $h }
        Start-Sleep -Milliseconds 250
    }
    return [IntPtr]::Zero
}

function Find-UnicornMessageBox {
    <#
      Find a dismissable UNICORN message box / confirmation, title-independent.

      Both the "Import was successful" box AND the file-open browser are titled
      'Import', so title matching is useless here. Distinguish by BUTTONS:
      an info/confirm/error box we must dismiss carries a button captioned
      OK / Yes / Close / Continue / Done; the file-open browser carries
      Open / Cancel (no such caption) and UNICORN's normal/owner-drawn windows
      carry no standard Win32 buttons at all. So "a top-level UNICORN window
      with a standard Dismiss-captioned button" is the reliable signature.
    #>
    param(
        [string[]]$DismissCaptions = @('OK', 'Yes', 'Close', 'Continue', 'Done'),
        [string]$ProcessName = 'UNICORNClient'
    )
    $p = Get-Process $ProcessName -ErrorAction SilentlyContinue
    if (-not $p) { return [IntPtr]::Zero }
    foreach ($h in [W32Dlg]::TopLevel($p.Id)) {
        $caps = @(Get-DialogButtons -Dialog $h | ForEach-Object { $_.Text })
        if ($caps | Where-Object { $DismissCaptions -contains $_ }) { return $h }
    }
    return [IntPtr]::Zero
}

function Wait-UnicornMessageBox {
    <# Wait for any dismissable UNICORN message box to appear; handle or Zero. #>
    param(
        [string[]]$DismissCaptions = @('OK', 'Yes', 'Close', 'Continue', 'Done'),
        [int]$TimeoutMs = 15000
    )
    $deadline = [Environment]::TickCount + $TimeoutMs
    while ([Environment]::TickCount -lt $deadline) {
        $h = Find-UnicornMessageBox -DismissCaptions $DismissCaptions
        if ($h -ne [IntPtr]::Zero) { return $h }
        Start-Sleep -Milliseconds 250
    }
    return [IntPtr]::Zero
}

function Close-UnicornMessageBox {
    <#
      Dismiss a message box by pressing the first available of the preferred
      captions (OK/Yes/Close/...), falling back to the first enabled button.
      Returns $true if the window closed. Reports what it said for the log.
    #>
    param(
        [Parameter(Mandatory)][IntPtr]$Dialog,
        [string[]]$PreferButtons = @('OK', 'Yes', 'Close', 'Continue', 'Done'),
        [int]$TimeoutMs = 6000
    )
    $btns = @(Get-DialogButtons -Dialog $Dialog)
    foreach ($cap in $PreferButtons) {
        $b = $btns | Where-Object { $_.Text -eq $cap -and $_.Enabled } | Select-Object -First 1
        if ($b) { return (Invoke-DialogButton -Dialog $Dialog -Text $cap -TimeoutMs $TimeoutMs) }
    }
    $b = $btns | Where-Object { $_.Enabled } | Select-Object -First 1
    if ($b) { return (Invoke-DialogButton -Dialog $Dialog -Text $b.Text -TimeoutMs $TimeoutMs) }
    return $false
}
