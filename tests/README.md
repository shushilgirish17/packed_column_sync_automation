# Tests

Unit tests for the **offline, deterministic** logic only. Anything that needs a
live UNICORN, WinAppDriver, or Benchling connection is not unit-tested here —
that surface is exercised by the scripts in `tools/` against the real app.

## What's covered

| Test file | Module under test | Focus |
|---|---|---|
| `test_unicorn_coltype.py` | `lib/unicorn_coltype.py` | CRC algorithms, manifest is byte-identical to real exports, verify/round-trip, corruption detection |
| `test_generate_import.py` | `tools/generate_import.py` | qualification gate, bed-height derivation, field mapping, `build_xml` (incl. the passed-record regression) |
| `test_benchling_pull.py` | `tools/benchling_pull.py` | mock record shape, resin parsing, `-Set` string, Active-only contract |
| `test_config.py` | `lib/config.py` | `.env` parser (comments, quotes, first-`=` split) |
| `pester/env.Tests.ps1` | `lib/env.ps1` | `Get-ProjectConfig` defaults + env-var override |

## Running

Python (from the repo root, in the venv):

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt   # pytest, etc.
pytest
```

PowerShell — the test uses **Pester 5** syntax. Windows ships the legacy
Pester 3.4, which cannot run it, so install/​update first:

```powershell
Install-Module Pester -MinimumVersion 5.0 -Scope CurrentUser -Force -SkipPublisherCheck
Import-Module Pester
Invoke-Pester tests\pester -Output Detailed
```

The Python tests read the sample archives in `fixtures/`; they create no files
outside pytest's temp dirs and never touch UNICORN.
