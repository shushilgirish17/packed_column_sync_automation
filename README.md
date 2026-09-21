# UNICORN Packed Column Sync — Automation

Automation for syncing **packed column** data from Benchling into Cytiva
**UNICORN**'s Column Type library.

UNICORN exposes no API for column data (confirmed with Cytiva), and its
Technical Manual advises against writing to the database directly. So this
project drives UNICORN through supported application features — **Import** and
**Edit** in the Column Handling dialog — using Windows UI Automation and
WinAppDriver. All values are read from Benchling and written through the
application, so UNICORN's own validation and audit trail stay intact.

## Two write paths (both verified end-to-end)

| Scenario | Path | How |
|---|---|---|
| Column type **does not exist** → create | **Import** | Generate a UNICORN import `.zip` from a Benchling record and import it via Column Handling → Import |
| Column type **exists** → update values | **Edit** | Select it, open Edit, set the writable cells, Save |

Reading the existing library (for dedup / reconciliation) is done by driving the
Column Handling list, not by querying the database.

## Layout

```
lib/                       Reusable modules (no side effects on import)
  wad.py                   Minimal WinAppDriver client (stdlib only)
  unicorn_coltype.py       Build & verify UNICORN column-type import archives
  win32_dialogs.ps1        Press UNICORN message-box buttons without a real mouse

tools/                     Runnable scripts
  benchling_pull.py        PRODUCER: Benchling entity+result -> gate on Active
                           -> build .zip -> drop job in the watch folder
  watch_service.ps1        CONSUMER: watch the folder, run sync_column per job
  sync_column.ps1          END-TO-END one column: search -> edit|import -> verify
  canary.ps1               Health check — run BEFORE any automation
  extract_column_types.ps1 Dump the whole Column Type library to CSV
  export_column_type.py    Export one column type to a .zip (inspect the format)
  generate_import.py       Map a Benchling record -> UNICORN fields -> import .zip

fixtures/                  Test data
  export_sample_a/b.zip    Real UNICORN exports (prove the archive format)
  corrupt_crc.zip          Bad-checksum archive (UNICORN rejects it)

docs/                      Write-ups and requests for stakeholders
exports/                   Runtime output (git-ignored)
```

Scripts locate `lib/` relative to their own path, so the project can live
anywhere — no absolute paths baked in.

## Prerequisites

- Windows, with **UNICORN** installed and the **Column Handling** dialog
  reachable (open it from System Control, Method Editor, or Evaluation Classic).
- **WinAppDriver** running and listening on `http://127.0.0.1:4723`
  (`"C:\Program Files (x86)\Windows Application Driver\WinAppDriver.exe"`).
  Needed for clicks; not needed for the offline `generate_import.py` or the
  `.NET`-UIA-only reads.
- **Python 3**. The pipeline uses the **standard library only** — no runtime
  packages to install.

## Setup

```powershell
# 1. Configuration — copy the template and adjust if needed
Copy-Item .env.example .env

# 2. Virtual environment (optional; only needed for the dev tooling below)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt   # pypdf, pillow — for the doc utilities only
```

`requirements.txt` is intentionally empty of runtime dependencies (see the file).
Configuration lives in **`.env`** (git-ignored; copy from `.env.example`) and is
read by both `lib/config.py` (Python) and `lib/env.ps1` (PowerShell):

| Variable | Default | Meaning |
|---|---|---|
| `WINAPPDRIVER_URL` | `http://127.0.0.1:4723` | WinAppDriver endpoint |
| `UNICORN_PROCESS` | `UNICORNClient` | UNICORN client process name |
| `COLUMN_DIALOG_TITLE` | `Column Handling` | dialog to attach to |
| `IMPORT_ACCESS_SCOPE` | `Personal` | scope for generated imports (`Personal`/`Global`) |
| `EXPORTS_DIR` | `<root>/exports` | where output is written |

Precedence: real environment variable > `.env` > built-in default.

## Usage

End-to-end sync of one column type (the whole flow in one command):

```powershell
# Update an existing column type's parameters, then verify
powershell -NoProfile -ExecutionPolicy Bypass -File tools\sync_column.ps1 `
    -ColumnName 'ZZTEST' -Set 'Default flow rate=2.5;Max flow rate=3.0'

# Create one from a generated import file (used when it doesn't exist yet)
powershell -NoProfile -ExecutionPolicy Bypass -File tools\sync_column.ps1 `
    -ColumnName 'ZZTEST-BenchlingSync-DELETE-ME 1x10cm 7.9mL' `
    -ImportZip exports\ZZTEST_import.zip
```

Or start with just the health check:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\canary.ps1
```

### Demo pipeline (local watch folder instead of AWS)

Until cloud is provisioned, the AWS half (Lambda/S3/SQS) is replaced by a local
watch folder. A manually-run producer generates jobs; a background watcher
consumes them and drives UNICORN.

```powershell
# 1. Producer — pull one Benchling record, gate on Column Status = Active,
#    and drop a job (.zip + .job.json) into <WATCH_DIR>/incoming.
python tools\benchling_pull.py --id COL0123      # real (needs Benchling creds)
python tools\benchling_pull.py --mock            # demo, no creds; Active sample
python tools\benchling_pull.py --mock-inactive   # shows the Active gate blocking

# 2. Consumer — watch the folder; for each job run sync_column (create/update/
#    verify), then move it to done/ or failed/ with a per-job log.
powershell -NoProfile -ExecutionPolicy Bypass -File tools\watch_service.ps1        # loop
powershell -NoProfile -ExecutionPolicy Bypass -File tools\watch_service.ps1 -Once  # one pass
```

Only **Active** columns produce a job; inactive/failed packs are logged and
skipped. The watcher processes one job at a time (single UNICORN desktop). In
production the folder becomes S3+SQS and the watcher becomes the Automation
Anywhere bot — the per-job logic (sync_column) is unchanged.

### Run it continuously (auto-triggered)

`watch_service.ps1` is event-driven: it blocks on a `FileSystemWatcher` and
fires as soon as a `.job.json` lands, with a periodic safety sweep (default 5 s)
so nothing is ever missed. To make it start automatically and stay running,
register it as a **scheduled task** (run from an elevated shell):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\install_watch_task.ps1
# remove with:  ... install_watch_task.ps1 -Uninstall
```

**It is registered as a Scheduled Task, not a Windows Service — on purpose.**
A Windows Service runs in Session 0, which cannot see or click UNICORN's UI.
The task runs in the **interactive logged-on session** (`LogonType Interactive`,
`RunLevel Highest`), which is the only context where the automation can drive
the desktop. Verified the hard way: the same watcher launched as a *detached*
process fails at the Edit click because a background process cannot bring
UNICORN to the foreground for the synthetic mouse click. Requirements:

- a **dedicated host kept logged on and unlocked** (UIA stops on a locked
  desktop),
- UNICORN + WinAppDriver running in that session,
- single display / primary monitor (WinAppDriver misses negative coordinates).

Generate an import file from a Benchling record (offline, safe):

```bash
python tools/generate_import.py        # edit RECORD in the file, or import build_xml()
```

Dump the current library for field mapping / dedup:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\extract_column_types.ps1 -Limit 25
```

Verify a generated archive is well-formed (matches UNICORN's checksums):

```bash
python lib/unicorn_coltype.py          # self-test against fixtures/
```

## Import archive format (reverse-engineered)

A column-type export/import is a plain **ZIP** (UNICORN 7.10, `FormatVersion="2"`):

- `ColumnType.xml` — UTF-8, no BOM. Root `<ColumnTypes>` is plural, so **one
  file can carry many column types** (batch import).
- `Manifest.xml` — two CRC-32 integrity checks. **UNICORN enforces these** —
  a mismatched checksum is rejected (`corrupt_crc.zip` demonstrates this).
  `unicorn_coltype.py` reproduces both, byte-identically to UNICORN's own output.

`<AccessScopeName>` controls visibility: **`Personal`** (only the creator sees
it — use for testing) or **`Global`** (everyone — production).

## Critical gotchas (learned the hard way — see the notes below)

- **Never call `InvokePattern.Invoke()` on a control that opens a modal.** It
  blocks UNICORN's entire UI Automation provider; killing the process while it
  is blocked corrupts the provider until UNICORN is restarted. Click via
  WinAppDriver (or synthetic mouse) instead. `canary.ps1` detects a corrupted
  provider.
- **A tab's controls exist in the UI tree only while that tab is selected.**
  Select the tab first.
- **List selection needs a real click**, not `SelectionItemPattern.Select()` —
  Select() moves the highlight but does not fire the event that repopulates the
  parameter grids.
- **WinAppDriver clicks miss at negative screen coordinates** (UNICORN on a
  left/upper secondary monitor). Use synthetic mouse, or move the window onto
  the primary monitor first.
- **Message-box buttons** (Yes/No, OK) are invisible to UIA. Press them with
  `SendMessage(btn, BM_CLICK)` — **not** `PostMessage` (silently ignored).
  See `lib/win32_dialogs.ps1`.

## Status

- Import (create) and Edit (update) both proven against the live client.
- Nine UNICORN-required fields map from Benchling as: 4 direct/constant, 5 from
  a maintained resin/hardware lookup. See `docs/` for the field mapping and the
  open requirements questions (naming convention, the resin lookup source,
  inactive-column handling).
