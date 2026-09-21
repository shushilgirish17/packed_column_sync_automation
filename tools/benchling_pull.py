"""
benchling_pull.py
-----------------
Manually-run producer for the demo pipeline (stands in for the AWS
Lambda/S3/SQS path until Lilly provisions cloud for this automation).

What it does:
  1. Read a Packed Column entity + its Column Packing result from Benchling.
  2. GATE on Column Status = Active. If not Active, print a message and stop -
     no file is produced (inactive/failed packs must never reach UNICORN).
  3. For an Active column, map the fields and build the UNICORN import .zip
     (reusing generate_import + unicorn_coltype).
  4. Drop the .zip and a small .job.json into the WATCH folder's `incoming/`.
     watch_service.ps1 picks it up and drives UNICORN.

Modes:
  --mock            Skip Benchling; use a built-in Active sample so the
                    watch-folder -> UNICORN half can be demoed with no creds.
  --mock-inactive   Same, but Status = Inactive, to show the gate blocking.
  --id <registryId> Real mode: pull that entity from Benchling (needs
                    BENCHLING_URL / BENCHLING_API_KEY in .env, and
                    `pip install benchling-sdk`).

Usage:
  python tools/benchling_pull.py --mock
  python tools/benchling_pull.py --id COL0123
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "lib"))
sys.path.insert(0, str(_ROOT / "tools"))
import unicorn_coltype
import generate_import as gi
from config import settings


# ---------------------------------------------------------------------------
# Field mapping - Benchling system-names to our record shape.
# Confirm these against the actual Packed Column schema before real use.
# ---------------------------------------------------------------------------
FIELD_MAP = {
    "resin_lot":            "resin_lot",
    "column_hardware":      "column_hardware",
    "column_type":          "column_type",
    "packing_method":       "packing_method",
    "packing_buffer_used":  "packing_buffer_used",
    "column_diameter_cm":   "column_diameter_cm",
    "packed_volume_ml":     "packed_volume_ml",
    "packed_bed_height_cm": "packed_bed_height_cm",
    "molecule":             "molecule",
    "packing_operator":     "packing_operator",
    "notes_comments":       "notes_comments",
    "resin_catalog_number": "resin_catalog_number",
}
# Result-table (Column Packing) fields
RESULT_MAP = {
    "asymmetry_qc":         "asymmetry_qc",
    "plates_m_qc":          "plates_m_qc",
    "column_pack_qc":       "column_pack_qc",
    "column_status":        "column_status",
    "theoretical_plates_m": "theoretical_platesmeter",
}


def mock_record(active: bool) -> dict:
    """A sample Active (or Inactive) record, shaped for generate_import.

    Uses the existing ZZTEST column's identity so the watch->UNICORN step
    updates that Personal-scope entry (safe, reversible) rather than creating
    something new.
    """
    return {
        "id":                   "ZZTEST-DEMO",
        "registry":             "Lilly PRD (mock)",
        "author":               "DEMO",
        "resin_lot":            "1L MabSelect PrismA-17549803-001 RMLOT060",
        "resin_type":           "MabSelect PrismA",
        "column_hardware":      ["150mm/10MM Omnifit Benchmark Glass Columns (with 2 x adjustable endpieces)"],
        "column_type":          None,
        "packing_method":       "BPG Column Packing",
        "packing_buffer_used":  "BTS26005-LB066-50 mM Tris, 1 M NaCl, pH 8",
        "column_diameter_cm":   1.0,
        "packed_volume_ml":     7.85398163397448,
        "packed_bed_height_cm": 10,
        "molecule":             None,
        "packing_operator":     "DEMO",
        "notes_comments":       "mock demo record",
        "asymmetry_qc":         "Pass",
        "plates_m_qc":          "Pass",
        "theoretical_plates_m": 10000,
        "column_pack_qc":       "Pass",
        "column_status":        ("Active" if active else "Inactive"),
        "resin_catalog_number": None,
        "hardware_code_number": None,
        # Demo shortcut: target the existing ZZTEST entry by name so this
        # exercises the UPDATE path against a safe Personal-scope column.
        "_override_name":       "ZZTEST-BenchlingSync-DELETE-ME 1x10cm 7.9mL",
    }


def fetch_from_benchling(registry_id: str) -> dict:
    """Real mode: pull the entity + result from Benchling into our shape.

    Imported lazily so --mock works without benchling-sdk installed.
    """
    try:
        from benchling_sdk.benchling import Benchling
        from benchling_sdk.auth.api_key_auth import ApiKeyAuth
    except ImportError:
        sys.exit("benchling-sdk not installed. Run: pip install benchling-sdk  "
                 "(or use --mock).")
    if not settings.benchling_url or not settings.benchling_api_key:
        sys.exit("BENCHLING_URL / BENCHLING_API_KEY not set in .env (or use --mock).")

    bench = Benchling(url=settings.benchling_url, auth_method=ApiKeyAuth(settings.benchling_api_key))

    # NOTE: schema-specific. Adjust the finder to your Packed Column schema
    # and Column Packing result schema. Left explicit so it is easy to confirm.
    entity = None
    for page in bench.custom_entities.list(name=registry_id):
        for e in page:
            entity = e
            break
        if entity:
            break
    if entity is None:
        sys.exit(f"No Packed Column entity found for '{registry_id}'.")

    f = entity.fields
    def fld(name):
        try:
            return f.get(name).value
        except Exception:
            return None

    rec = {"id": registry_id, "registry": "Benchling", "author": "",
           "resin_type": None, "hardware_code_number": None}
    for ours, bname in FIELD_MAP.items():
        rec[ours] = fld(bname)
    # resin_type is typically derived from the resin lot's linked resin;
    # fall back to parsing the lot string.
    if not rec.get("resin_type") and rec.get("resin_lot"):
        rec["resin_type"] = _parse_resin(rec["resin_lot"])

    # Column Packing result - fetch the latest result row for this entity and
    # copy the QC + status fields. (Assay-result retrieval is schema-specific;
    # implement against your result schema id here.)
    result = _fetch_latest_result(bench, entity)
    for ours, bname in RESULT_MAP.items():
        rec[ours] = (result.get(bname) if result else None)
    return rec


def _parse_resin(resin_lot: str) -> str:
    # "1L MabSelect PrismA-17549803-001 RMLOT060" -> "MabSelect PrismA"
    import re
    m = re.search(r"([A-Za-z][A-Za-z0-9 ]+?)-\d", resin_lot)
    return (m.group(1).split(maxsplit=1)[-1].strip() if m else resin_lot).strip()


def _fetch_latest_result(bench, entity) -> dict | None:
    # Placeholder for the result-schema-specific query. Return a dict of
    # RESULT_MAP source field-names to values, or None. Implement against the
    # Column Packing result schema when wiring real Benchling.
    return None


def set_string(run_rows) -> str:
    """The writable Run-Parameter fields, as sync_column's -Set string."""
    wanted = {"Max pre-column pressure", "Max delta column pressure",
              "Default flow rate", "Max flow rate"}
    parts = [f"{f}={v}" for f, v, _src in run_rows if f in wanted and v not in ("", None)]
    return ";".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", help="Benchling registry id to pull")
    ap.add_argument("--mock", action="store_true", help="use a built-in Active sample")
    ap.add_argument("--mock-inactive", action="store_true", help="built-in Inactive sample")
    args = ap.parse_args()

    if args.mock or args.mock_inactive:
        rec = mock_record(active=not args.mock_inactive)
    elif args.id:
        rec = fetch_from_benchling(args.id)
    else:
        ap.error("provide --id <registryId>, or --mock / --mock-inactive")

    ident = rec.get("id", "?")
    status = rec.get("column_status")
    print(f"Packed Column: {ident}   Column Status: {status!r}")

    # --- THE GATE -----------------------------------------------------------
    if status != "Active":
        print(f"Column status of '{ident}' is {status!r} (not Active) - "
              f"skipping. No file generated.")
        return 0

    # extra safety: the agreed qualification gate
    ok, reasons = gi.sync_gate(rec)
    if not ok:
        print(f"'{ident}' is Active but failed qualification: {', '.join(reasons)} - skipping.")
        return 0

    # --- build the import artifact -----------------------------------------
    name, h, run, details, ordering = gi.build_rows(rec)
    name = rec.get("_override_name") or name
    xml = gi.build_xml(rec, name, h, scope=settings.import_access_scope)

    incoming = settings.watch_dir / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    stem = f"{ident}_{time.strftime('%Y%m%d-%H%M%S')}"
    zip_path = incoming / f"{stem}.zip"
    unicorn_coltype.build_archive(xml, str(zip_path))

    job = {
        "columnName": name,
        "set":        set_string(run),
        "zip":        zip_path.name,
        "source":     ident,
        "scope":      settings.import_access_scope,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    job_path = incoming / f"{stem}.job.json"
    job_path.write_text(json.dumps(job, indent=2), encoding="utf-8")

    print(f"Active - generated job for UNICORN:")
    print(f"  column : {name}")
    print(f"  set    : {job['set']}")
    print(f"  zip    : {zip_path}")
    print(f"  job    : {job_path}")
    print(f"  checks : {unicorn_coltype.verify_archive(str(zip_path)) or 'VALID'}")
    print(f"\nDropped into {incoming} - watch_service.ps1 will pick it up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
