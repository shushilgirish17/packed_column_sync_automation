"""
export_column_type.py
---------------------
Export a UNICORN column type to a .zip, driven entirely by automation.

Nobody touches the mouse or keyboard. The script finds the dialog, selects the
Column Type Parameters tab, picks a column type, clicks Export, completes the
Save dialog, and verifies the resulting file.

Read-only with respect to UNICORN data: exporting writes a file to disk and
changes nothing in the application or the database.

Usage:
  python export_column_type.py [output_path_without_extension]
"""

from __future__ import annotations

import ctypes
import pathlib
import subprocess
import sys
import time
from ctypes import wintypes

# Locate the reusable modules in ../lib relative to this script (not a
# hardcoded path), so the project can live anywhere.
_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "lib"))
import wad
from config import settings

DEFAULT_OUT = str(settings.exports_dir / "export")
DIALOG = settings.column_dialog_title
GW_OWNER = 4

user32 = ctypes.WinDLL("user32", use_last_error=True)
EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [EnumProc, wintypes.LPARAM]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND


def step(n, total, msg):
    print(f"\n[{n}/{total}] {msg}")


def windows_of(pid):
    """{hwnd: title} for visible titled windows of a process."""
    out = {}

    def cb(h, _):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(h):
            b = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(h, b, 512)
            if b.value:
                out[h] = b.value
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return out


def unicorn_pid():
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"(Get-Process {settings.unicorn_process} -ErrorAction SilentlyContinue).Id"],
                       capture_output=True, text=True)
    lines = [l for l in r.stdout.strip().splitlines() if l.strip()]
    return int(lines[0]) if lines else None


def find_dialog(pid):
    return next((h for h, t in windows_of(pid).items() if t == DIALOG), None)


def owner_of(hwnd):
    """The unowned top-level window that owns `hwnd`.

    Resolved via GW_OWNER rather than by title: the Evaluation window's
    caption changes when a result file is open (e.g. 'Evaluation Classic -
    [CEX_gradient_fractions_LB2273 (Read-Only)]'), so title matching breaks.
    """
    o = user32.GetWindow(hwnd, GW_OWNER)
    return o if o else None


def sendkeys_to(hwnd, text):
    """Type into `hwnd`, refusing unless it is genuinely foreground."""
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "Add-Type @'\nusing System;using System.Runtime.InteropServices;"
        'public class Z{[DllImport("user32.dll")]public static extern IntPtr GetForegroundWindow();'
        '[DllImport("user32.dll")]public static extern bool SetForegroundWindow(IntPtr h);}\n\'@; '
        f"[void][Z]::SetForegroundWindow([IntPtr]{hwnd}); Start-Sleep -Milliseconds 800; "
        f"if([Z]::GetForegroundWindow() -ne [IntPtr]{hwnd}){{'ABORT: dialog not foreground'; exit 1}}; "
        "[System.Windows.Forms.SendKeys]::SendWait('^a'); Start-Sleep -Milliseconds 200; "
        f"[System.Windows.Forms.SendKeys]::SendWait('{text}'); Start-Sleep -Milliseconds 400; "
        "[System.Windows.Forms.SendKeys]::SendWait('{ENTER}'); 'ok'"
    )
    return subprocess.run(["powershell", "-NoProfile", "-Command", script],
                          capture_output=True, text=True).stdout.strip()


def main() -> int:
    out_base = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUT
    pathlib.Path(out_base).parent.mkdir(parents=True, exist_ok=True)
    total = 5

    step(1, total, "Checking UNICORN and WinAppDriver")
    url = settings.winappdriver_url
    pid = unicorn_pid()
    if not pid:
        print(f"    {settings.unicorn_process} is not running.")
        return 2
    try:
        wad.status(url)
    except wad.WadError as exc:
        print(f"    WinAppDriver not reachable: {exc}")
        return 2
    print(f"    {settings.unicorn_process} pid={pid}, WinAppDriver up.")

    step(2, total, f"Locating the '{DIALOG}' dialog")
    dlg = find_dialog(pid)
    if not dlg:
        print("    Not open - opening it via automation ...")
        opened = False
        for hwnd, title in windows_of(pid).items():
            if user32.GetWindow(hwnd, GW_OWNER):
                continue  # owned popup, not a session target
            try:
                s = wad.attach(hwnd, url)
            except wad.WadError:
                continue
            try:
                btn = s.find("xpath", f"//Button[@Name='{DIALOG}']")
                s.click(btn)
                opened = True
            except wad.WadError:
                continue
            finally:
                s.close()
            if opened:
                break
        for _ in range(20):
            time.sleep(0.5)
            dlg = find_dialog(pid)
            if dlg:
                break
    if not dlg:
        print(f"    Could not open '{DIALOG}'.")
        return 1
    owner = owner_of(dlg)
    print(f"    dialog hwnd=0x{dlg:x}, owner hwnd=0x{owner:x}")
    print(f"    owner title: {windows_of(pid).get(owner)!r}")

    s = wad.attach(owner, url)
    try:
        step(3, total, "Selecting the Column Type Parameters tab and a column type")
        tab = s.find("xpath", "//TabItem[@Name='Column Type Parameters']")
        s.click(tab)
        # tab pages populate lazily; wait for a tab-scoped control to appear
        btn = None
        for _ in range(15):
            time.sleep(1.0)
            try:
                btn = s.find("id", "btnExportType")
                break
            except wad.ElementNotFound:
                continue
        if not btn:
            print("    Export button never appeared.")
            return 1

        items = s.find_all("xpath", "//List[@Name]//ListItem")
        if items:
            s.click(items[0])
            time.sleep(1.2)
            print(f"    selected: {s.attr(items[0], 'Name')!r}  "
                  f"({len(items)} types listed)")

        step(4, total, "Clicking Export... (automated)")
        before = set(windows_of(pid))
        t0 = time.time()
        s.click(btn)
        print(f"    click returned in {time.time() - t0:.2f}s")

        save = None
        for _ in range(30):
            time.sleep(0.5)
            new = {h: t for h, t in windows_of(pid).items() if h not in before}
            hit = [h for h, t in new.items() if "Export" in t]
            if hit:
                save = hit[0]
                break
        if not save:
            print("    Save dialog did not appear.")
            return 1
        print(f"    '{windows_of(pid)[save]}' dialog opened")

        step(5, total, "Completing the Save dialog and verifying the file")
        print(f"    {sendkeys_to(save, out_base)}")
        time.sleep(2.5)
    finally:
        s.close()

    import os
    path = out_base + ".zip"
    if not os.path.exists(path):
        print(f"    Expected {path} - not found.")
        return 1
    print(f"    wrote {path} ({os.path.getsize(path):,} bytes)")

    import unicorn_coltype
    problems = unicorn_coltype.verify_archive(path)
    print(f"    checksums: {problems or 'VALID'}")

    import re
    import zipfile
    with zipfile.ZipFile(path) as z:
        print(f"    contains: {', '.join(z.namelist())}")
        xml = z.read("ColumnType.xml").decode("utf-8")
    name = re.search(r"<Name>(.*?)</Name>", xml).group(1)
    scope = re.search(r"<AccessScopeName>(.*?)</AccessScopeName>", xml).group(1)
    print(f"\n    Exported column type : {name}")
    print(f"    Access scope         : {scope}")
    print("\nDone - exported by automation, no manual interaction.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
