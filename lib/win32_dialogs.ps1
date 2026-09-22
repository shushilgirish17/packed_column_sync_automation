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
