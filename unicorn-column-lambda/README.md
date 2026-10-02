# unicorn-column-lambda

Upstream half of *ELYC_Unicorn_Column_Management*: reads Packed Columns from Benchling, resolves the
Resin Characteristics Library, applies the mapping from `Unicorn_mapping_fields.xlsx`, builds the UNICORN
Column Type import archive to S3, and queues one SQS message per column for the on-prem UNICORN side
(`unicorn_automation_test`: `sync_column.ps1`, WinAppDriver).

```
Benchling canvas --webhook--> Lambda --> S3 (<stem>.zip + <stem>.job.json) + SQS (one message per column)
                                                                         --> on-prem consumer --> UNICORN
                              |  credentials: Secrets Manager
                              |  audit + errors: CloudWatch Logs
```

AWS services: **Lambda, Secrets Manager, S3, SQS, CloudWatch Logs** - all created by
`infra/template.yaml` (see [infra/README.md](infra/README.md)).

## Layout
```
lambda_function.py            event routing: webhook (Function URL) / async self-invoke / manual test
config.py                     schema IDs, field names, technique map, UNICORN settings (all env-driven)
auth_utils/authentication.py  Benchling client, credentials from SSM
canvas_handler/               App Canvas in the entry: pick columns -> "Sync to UNICORN" -> per-column results
sync/
  benchling_reader.py         read columns + hardware, resolve resin library row
  validate.py                 mandatory fields
  mapping.py                  derivations, bar->MPa, pH, technique, column type name
  unicorn_archive.py          UNICORN ZIP + Manifest.xml CRCs (ported from unicorn_coltype.py)
  import_file.py              ColumnType.xml + job -> S3 + SQS (or ./out/incoming locally)
  packed_column_sync.py       orchestrator; one audit log line per column
tools/                        local only: inspect_schemas.py, run_local.py (load .env)
tests/                        ZZTEST mapping, archive CRCs vs real UNICORN exports, SQS message
build.py                      builds dist/ zips (layer + code)
```

## SQS message (one per column)
```json
{ "columnName": "...", "set": "Max pre-column pressure=0.3;...", "zip": "COL010_..._ab12cd34.zip",
  "source": "COL010", "scope": "Personal", "generatedAt": "...",
  "contentHash": "...", "placeholders": [...], "warnings": [...],
  "s3": { "bucket": "...", "zipKey": "unicorn-sync/incoming/<stem>.zip", "jobKey": "unicorn-sync/incoming/<stem>.job.json" } }
```
The first six keys are the job contract `sync_column.ps1` already uses. The consumer downloads `s3.zipKey`,
then: column type missing -> Import; present -> Edit with `set`. With a `.fifo` queue, jobs are
delivered one at a time and identical re-sends within 5 minutes are dropped.

Rules:
- Only **Active** columns are queued. Inactive -> skipped (logged), never sent.
- `ColumnType.xml` carries only elements confirmed from real exports (Name, AccessScopeName, BedHeight,
  Diameter, Default/Max flow, Pre/Delta pressure, TechniqueName). UNICORN derives volume and linear flow.
- Flow/pressure are NOT NULL in UNICORN: a missing value is written as `0` (agreed placeholder, warned)
  and is never included in `set`, so an Edit can't overwrite a real value with 0.

## Audit / troubleshooting (CloudWatch)
Each column logs one line `{"audit": {...}}` (entity, registry id, column type name, result, error,
SQS message id, user, entry). In CloudWatch Logs Insights:
```
fields @timestamp, audit.benchling_registry_id, audit.result, audit.message
| filter ispresent(audit.result) | sort @timestamp desc
```

## Build and deploy
```bash
python build.py          # dist/packed-column-sync-layer.zip + dist/packed-column-sync.zip
python build.py --code   # after a code change: code zip only
```
Layer zip -> Lambda layer (Python 3.14, x86_64). Code zip -> the function. Rebuild the layer only when
`requirements.txt` changes. Deploy with CloudFormation: [infra/README.md](infra/README.md).

Environment variables (set by the template): `BENCHLING_SECRET_ID` (Secrets Manager, holds
`client_secret`) + `BENCHLING_URL` + `BENCHLING_CLIENT_ID`, `PACKED_COLUMN_SCHEMA_ID`, `RESIN_LIBRARY_SCHEMA_ID`,
`IMPORT_BUCKET`, `SQS_QUEUE_URL`, and for the canvas webhook `APP_DEFINITION_ID`. Optional: `IMPORT_ACCESS_SCOPE`
(`Personal` default, `Global` for production), `COLUMN_NAME_TEMPLATE`, `TECHNIQUE_MAP`.
The hand-built Zifo function still uses `BENCHLING_SSM_PARAMETER` (SSM SecureString with all three values);
the code accepts either.

## Local run (no AWS)
```bash
pip install -r requirements-dev.txt && pytest -q
cp .env.example .env                       # Benchling creds + schema IDs
python -m tools.inspect_schemas "Packed Column" "Resin Characteristics"
python -m tools.run_local bfi_xxxxx        # writes ./out/incoming/<stem>.zip + .job.json
```

## Open items
- TechniqueName: `Affinity`, `CationExchange` confirmed; `AnionExchange`, `HydrophobicInteraction`,
  `GelFiltration`, `ReversedPhase` assumed - confirm with one export each.
- pH, particle size, ordering info: mapped and sent in `parameters`, not in the XML until their element
  names are seen in an export.
- Naming convention: `{resin}_{d} x {h}cm_{v}mL_{registry_id}`, e.g. `Poros 50 HS_1.0 x 20.3cm_17.708mL_COL001` (`COLUMN_NAME_TEMPLATE`). A name change creates a new column type rather than updating the existing one, since the name is the dedup key.
- Max linear flow: from the column's `Max Linear Flow Rate (cm/h)`; if empty, written empty (`MAX_LINEAR_FLOW_FACTOR=0.75` enables the sheet's interim rule).
- Pressure: `Max Pre-Column Pressure` is used as entered, in the column's `Pressure Unit` (bar or MPa); the resin library
  `max_pressure_bar` becomes Max delta column pressure (kept for bar, / 10 for MPa). UNICORN's XML token for bar is
  assumed `bar` - confirm with one export of a bar column type.
- Lilly tenant IP allowlist: the Lambda needs a VPC + NAT with an allowlisted IP there.
