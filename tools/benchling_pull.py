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
# Benchling field names, from the authoritative Unicorn_mapping_fields sheet.
#   ENTITY fields (Packed Column, schema ts_AJliSA2awf) use DISPLAY LABELS.
#   RESULT fields (Column Packing / Resin Characteristics Library) use the
#   warehouse/SYSTEM names.
# ---------------------------------------------------------------------------
# Packed Column entity (display labels)
E_DIAMETER   = "Column Diameter (cm)"
E_BED_HEIGHT = "Packed Bed Height (cm)"     # also present per-pack on the result
E_VOLUME     = "Packed Volume (mL)"         # alt to result calculated_volume_ml
E_RESIN_LOT  = "Resin Lot"
E_RESIN_TYPE = "Resin Type"
E_RESIN_CAT  = "Resin Catalog Number"
E_HARDWARE   = "Column Hardware"            # link -> Non-SAP Raw Material
E_LIN_FLOW   = "Column Operation Flow Rate (cm/h)"

# Column Packing result (system names)
R_VOLUME     = "calculated_volume_ml"
R_BED_HEIGHT = "packed_bed_height_cm"
R_STATUS     = "column_status"
R_PACK_QC    = "column_pack_qc"
R_ASYM_QC    = "asymmetry_qc"
R_PLATES     = "theoretical_platesmeter"

# Resin Characteristics Library result (system names) - looked up by resin.
RC_MAX_PRESS = "max_pressure_bar"
RC_PARTICLE  = "mean_particle_size_um"
RC_FLOW      = "suggested_flow_rate_cmhr"
RC_PH_RANGE  = "stable_ph_range"


def _num(v):
    """Coerce a Benchling value to float when it looks numeric, else pass through."""
    if v is None or isinstance(v, (int, float)):
        return v
    try:
        return float(str(v).strip())
    except (ValueError, TypeError):
        return v


def map_to_record(*, registry_id, entity_name, entity_fields, packing_fields,
                   resin_fields=None, hardware_name=None, hardware_code=None):
    """PURE mapping: raw Benchling field dicts -> the record generate_import wants.

    No SDK, no network - just dict-in, dict-out, so it is fully unit-testable.
    `entity_fields` is keyed by display label, `packing_fields`/`resin_fields`
    by system name. The transport layer below produces those plain dicts.
    """
    resin_fields = resin_fields or {}
    e, p = entity_fields, packing_fields

    # resin_type must resolve to a RESIN_DEFAULTS key, or build_rows KeyErrors.
    # Benchling's spelling can differ from the lookup key only by spacing/case
    # (e.g. 'POROS 50 HS' vs 'POROS 50HS'), so canonicalise before/after the
    # lot-parse fallback.
    resin_type = _canonical_resin(e.get(E_RESIN_TYPE))
    if resin_type not in gi.RESIN_DEFAULTS:
        parsed = _canonical_resin(_parse_resin(e.get(E_RESIN_LOT) or ""))
        resin_type = parsed if parsed in gi.RESIN_DEFAULTS else (resin_type or parsed)

    hw = [hardware_name] if hardware_name else (
        e.get(E_HARDWARE) if isinstance(e.get(E_HARDWARE), list) else
        ([e.get(E_HARDWARE)] if e.get(E_HARDWARE) else []))

    return {
        "id":                   registry_id,
        "registry":             "Benchling",
        "author":               "",
        "resin_lot":            e.get(E_RESIN_LOT),
        "resin_type":           resin_type,
        "column_hardware":      hw,
        "column_diameter_cm":   _num(e.get(E_DIAMETER)),
        "packed_volume_ml":     _num(p.get(R_VOLUME) if p.get(R_VOLUME) is not None
                                     else e.get(E_VOLUME)),
        "packed_bed_height_cm": _num(p.get(R_BED_HEIGHT) if p.get(R_BED_HEIGHT) is not None
                                     else e.get(E_BED_HEIGHT)),
        "molecule":             None,
        # --- Column Packing result: the gate fields ---
        "column_status":        p.get(R_STATUS),
        "column_pack_qc":       p.get(R_PACK_QC),
        "asymmetry_qc":         p.get(R_ASYM_QC),
        "theoretical_plates_m": _num(p.get(R_PLATES)),
        # --- Ordering / gaps ---
        "resin_catalog_number": e.get(E_RESIN_CAT),
        "hardware_code_number": hardware_code,
        # carried for traceability / future derivations
        "_entity_name":         entity_name,
        "_resin_particle_um":   _num(resin_fields.get(RC_PARTICLE)),
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


def _make_client():
    """Build a Benchling client (App OAuth2 if configured, else user API key).

    Imported lazily so --mock works without benchling-sdk installed.
    """
    try:
        from benchling_sdk.benchling import Benchling
        from benchling_sdk.auth.api_key_auth import ApiKeyAuth
    except ImportError:
        sys.exit("benchling-sdk not installed. Run: pip install benchling-sdk  "
                 "(or use --mock).")
    if not settings.benchling_url:
        sys.exit("BENCHLING_URL not set in .env (or use --mock).")
    if settings.benchling_client_id and settings.benchling_client_secret:
        from benchling_sdk.auth.client_credentials_oauth2 import ClientCredentialsOAuth2
        auth = ClientCredentialsOAuth2(settings.benchling_client_id,
                                       settings.benchling_client_secret)
    elif settings.benchling_api_key:
        auth = ApiKeyAuth(settings.benchling_api_key)
    else:
        sys.exit("No Benchling credentials: set BENCHLING_API_KEY or "
                 "BENCHLING_CLIENT_ID/SECRET in .env (or use --mock).")
    return Benchling(url=settings.benchling_url, auth_method=auth)


def _field_value(fields, name):
    """Raw value of one field (link fields return the linked id / list of ids)."""
    if fields is None:
        return None
    f = fields.get(name)
    return getattr(f, "value", None) if f is not None else None


def _scalar(f):
    """Best readable scalar for a field. Numbers keep their true numeric value;
    dropdown / link fields (whose .value is an option/entity id like
    'sfso_...' / 'bfi_...') resolve to .display_value (e.g. 'Active', 'Pass')."""
    if f is None:
        return None
    v = getattr(f, "value", None)
    if isinstance(v, (int, float)):
        return v
    dv = getattr(f, "display_value", None)
    return dv if dv not in (None, "") else v


def _efield(fields, name):
    """Readable scalar for a field by name (see _scalar)."""
    return _scalar(fields.get(name)) if fields is not None else None


def _field_keys(fields):
    """Field names present on an SDK Fields object."""
    if fields is None:
        return []
    keys = getattr(fields, "additional_keys", None)
    if keys:
        return list(keys)
    try:
        return list(fields.to_dict().keys())
    except Exception:
        return []


def _result_fields_dict(fields, names) -> dict:
    """Extract result field values into a plain dict. Uses the display-aware
    reader so dropdown fields (column_status, *_qc) come back as 'Active' /
    'Pass' rather than their 'sfso_...' option ids; numbers stay numeric."""
    return {n: _efield(fields, n) for n in names}


def _first(page_iter):
    for page in page_iter:
        for item in page:
            return item
    return None


def _latest_entity(bench, schema_id):
    """Newest registered Packed Column entity (server-side createdAt:desc)."""
    return _first(bench.custom_entities.list(schema_id=schema_id, sort="createdAt:desc"))


def _entity_by_id(bench, schema_id, registry_id):
    e = _first(bench.custom_entities.list(schema_id=schema_id, name=registry_id))
    return e or _first(bench.custom_entities.list(name=registry_id))  # fallback


def _latest_result_fields(bench, schema_id, entity, names) -> dict:
    """Latest assay result of `schema_id` FOR this entity, as a plain dict.

    Uses the SDK's server-side entity_ids filter + createdAt:desc sort, so the
    first row is the newest result linked to this Packed Column.
    """
    if not schema_id:
        return {}
    ent_id = getattr(entity, "id", None)
    r = _first(bench.assay_results.list(
        schema_id=schema_id,
        entity_ids=[ent_id] if ent_id else None,
        sort="createdAt:desc"))
    return _result_fields_dict(getattr(r, "fields", None), names) if r else {}


def _resolve_hardware(bench, hardware_ref):
    """Traverse the Column Hardware link to a (name, catalog#) pair.

    hardware_ref is the entity's 'Column Hardware' raw value: a linked id or a
    list of ids. Prefers the entry whose name looks like a column body (not a
    frit or a resin lot). Catalog # lives on the linked Non-SAP Raw Material.
    """
    ids = hardware_ref if isinstance(hardware_ref, list) else (
        [hardware_ref] if hardware_ref else [])
    name, code = None, None
    for hid in ids:
        if not isinstance(hid, str):
            continue
        try:
            hw = bench.custom_entities.get_by_id(hid)
        except Exception:
            continue
        nm = getattr(hw, "name", None)
        if not nm:
            continue
        low = nm.lower()
        cat = _efield(getattr(hw, "fields", None), "Catalog #")
        if "column" in low and "frit" not in low and not low.startswith("1l "):
            return nm, cat          # best match: a column body
        if name is None:
            name, code = nm, cat    # otherwise keep the first as fallback
    return name, code


def fetch_from_benchling(registry_id: str | None = None, *, latest: bool = False) -> dict:
    """Real mode: pull the entity + its result rows from Benchling into our shape.

    Pass latest=True to pull the newest registered Packed Column, or a
    registry_id for a specific one. Transport only - the field mapping lives in
    the pure map_to_record() so it can be unit-tested without creds.
    """
    bench = _make_client()
    entity = (_latest_entity(bench, settings.benchling_entity_schema) if latest
              else _entity_by_id(bench, settings.benchling_entity_schema, registry_id))
    if entity is None:
        sys.exit("No Packed Column entity found "
                 f"({'latest' if latest else registry_id!r}).")
    return _record_from_entity(bench, entity)


def _record_from_entity(bench, entity) -> dict:
    """Shared transport->record path for both latest and by-id fetches."""
    fields = getattr(entity, "fields", None)
    entity_fields = {n: _efield(fields, n) for n in
        (E_DIAMETER, E_BED_HEIGHT, E_VOLUME, E_RESIN_LOT, E_RESIN_TYPE,
         E_RESIN_CAT, E_HARDWARE, E_LIN_FLOW)}
    packing = _latest_result_fields(bench, settings.benchling_packing_schema, entity,
        (R_VOLUME, R_BED_HEIGHT, R_STATUS, R_PACK_QC, R_ASYM_QC, R_PLATES))
    resin = _latest_result_fields(bench, settings.benchling_resin_schema, entity,
        (RC_MAX_PRESS, RC_PARTICLE, RC_FLOW, RC_PH_RANGE))
    # Hardware needs the raw linked id(s), not the display label, to traverse.
    hw_name, hw_code = _resolve_hardware(bench, _field_value(fields, E_HARDWARE))
    return map_to_record(
        registry_id=(getattr(entity, "entity_registry_id", None)
                     or getattr(entity, "name", None) or "?"),
        entity_name=getattr(entity, "name", None),
        entity_fields=entity_fields, packing_fields=packing,
        resin_fields=resin, hardware_name=hw_name, hardware_code=hw_code)


def _parse_resin(resin_lot: str) -> str:
    # "1L MabSelect PrismA-17549803-001 RMLOT060" -> "MabSelect PrismA"
    import re
    m = re.search(r"([A-Za-z][A-Za-z0-9 ]+?)-\d", resin_lot)
    return (m.group(1).split(maxsplit=1)[-1].strip() if m else resin_lot).strip()


def _canonical_resin(name):
    """Map a Benchling resin label onto the RESIN_DEFAULTS key it matches,
    ignoring spacing and case ('POROS 50 HS' -> 'POROS 50HS'). Returns the
    original (trimmed) string if nothing matches, so the caller can report it."""
    if not name:
        return name
    def norm(s):
        return "".join(str(s).split()).lower()
    target = norm(name)
    for key in gi.RESIN_DEFAULTS:
        if norm(key) == target:
            return key
    return str(name).strip()


def set_string(run_rows) -> str:
    """The writable Run-Parameter fields, as sync_column's -Set string."""
    wanted = {"Max pre-column pressure", "Max delta column pressure",
              "Default flow rate", "Max flow rate"}
    parts = [f"{f}={v}" for f, v, _src in run_rows if f in wanted and v not in ("", None)]
    return ";".join(parts)


def _dump_fields(label, fields):
    print(f"  {label}:")
    names = _field_keys(fields)
    if not names:
        print("    (no fields)")
        return
    for n in names:
        f = fields.get(n)
        val = getattr(f, "value", None)
        disp = getattr(f, "display_value", None)
        extra = f"  (display={disp!r})" if disp not in (None, "", val) else ""
        print(f"    {n!r:36} = {val!r}{extra}")


def inspect(latest: bool, registry_id: str | None) -> int:
    """Print the live entity + its result rows so field names/linkage can be
    confirmed against the schema. Nothing is written; read-only."""
    bench = _make_client()
    entity = (_latest_entity(bench, settings.benchling_entity_schema) if latest
              else _entity_by_id(bench, settings.benchling_entity_schema, registry_id))
    if entity is None:
        print("No entity found."); return 1
    print("ENTITY")
    print(f"  id                 = {getattr(entity, 'id', None)!r}")
    print(f"  name               = {getattr(entity, 'name', None)!r}")
    print(f"  entity_registry_id = {getattr(entity, 'entity_registry_id', None)!r}")
    print(f"  created_at         = {getattr(entity, 'created_at', None)!r}")
    _dump_fields("entity.fields", getattr(entity, "fields", None))

    ent_id = getattr(entity, "id", None)
    for tag, sid in (("Column Packing", settings.benchling_packing_schema),
                     ("Resin Characteristics Library", settings.benchling_resin_schema)):
        print(f"\nRESULTS - {tag}  (schema {sid}, entity_ids=[{ent_id!r}])")
        found = 0
        for page in bench.assay_results.list(
                schema_id=sid, entity_ids=[ent_id] if ent_id else None,
                sort="createdAt:desc"):
            for r in page:
                print(f"  result id={getattr(r,'id',None)!r} created={getattr(r,'created_at',None)!r}")
                if found == 0:
                    _dump_fields("    fields", getattr(r, "fields", None))
                found += 1
                if found >= 3:
                    break
            if found >= 3:
                break
        if not found:
            print("  (no results linked to this entity for this schema - "
                  "the result may link by a different id; try without the filter)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", help="Benchling registry id to pull")
    ap.add_argument("--latest", action="store_true",
                    help="pull the newest registered Packed Column entity")
    ap.add_argument("--inspect", action="store_true",
                    help="read-only: dump the live entity + result field names/values")
    ap.add_argument("--mock", action="store_true", help="use a built-in Active sample")
    ap.add_argument("--mock-inactive", action="store_true", help="built-in Inactive sample")
    args = ap.parse_args()

    if args.inspect:
        return inspect(latest=(args.latest or not args.id), registry_id=args.id)

    if args.mock or args.mock_inactive:
        rec = mock_record(active=not args.mock_inactive)
    elif args.latest:
        rec = fetch_from_benchling(latest=True)
    elif args.id:
        rec = fetch_from_benchling(args.id)
    else:
        ap.error("provide --latest, --id <registryId>, --inspect, or --mock / --mock-inactive")

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

    # The UNICORN parameters technique/pressures/flow come from the maintained
    # per-resin lookup. When a resin has no entry (most registered columns have
    # no resin values), we DO NOT fabricate those safety limits - the column is
    # still created with those parameter cells left blank. Just note it.
    resin = rec.get("resin_type")
    if resin not in gi.RESIN_DEFAULTS:
        print(f"  note: resin {resin!r} has no parameter lookup - "
              f"technique/pressure/flow will be left blank in UNICORN.")

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
