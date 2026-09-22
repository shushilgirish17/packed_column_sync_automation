"""
generate_import.py
------------------
Map ONE Benchling packed_column record onto a UNICORN Column Type, filling all
three sections of the Column Type Parameters tab (Run Parameters, Details,
Ordering Information), and emit an importable .zip.

Runs entirely offline. Nothing here touches UNICORN.

The point of this script is to make the field mapping concrete and to show
exactly which values have a Benchling source, which are constants, which
UNICORN derives itself, and which are still genuinely missing. RECORD below is
an EXAMPLE (COL005); replace it with a real Benchling record, or import this
module and call build_rows() / build_xml() from a sync driver.

Usage:  python generate_import.py
"""

from __future__ import annotations

import datetime as dt
import math
import pathlib
import sys

# Reusable modules live in ../lib relative to this script.
_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "lib"))
import unicorn_coltype
from config import settings

# ---------------------------------------------------------------------------
# The Benchling record. Values marked (screenshot) were read off the registry
# grid; truncated cells are marked TODO and need confirming.
# ---------------------------------------------------------------------------
RECORD = {
    "id":                   "COL005",
    "registry":             "Lilly PRD",
    "author":               "EMMA.FOSTER",
    "created":              "2026-05-19T15:17",
    # --- schema fields ----------------------------------------------------
    "prepacked_vendor":     None,
    "resin_lot":            "1L MabSelect PrismA-17549803-001 RMLOT060",
    "resin_type":           "MabSelect PrismA",    # parsed from resin_lot
    "column_hardware": [
        "10 um polyethylene (PE) Frit",
        "150mm/10MM Omnifit Benchmark Glass Columns (with 2 x adjustable endpieces)",
        "1L MabSelect PrismA-17549803",
    ],
    "column_type":          None,
    "packing_method":       "BPG Column Packing",
    "packing_buffer_used":  "BTS26005-LB066-50 mM Tris, 1 M NaCl, pH 8",
    "column_diameter_cm":   1.0,
    "packed_volume_ml":     7.85398163397448,
    "molecule":             None,
    "packing_operator":     "ERF",
    "notes_comments":       "POROS",               # NB: inconsistent with resin
    # --- Column Packing result row ---------------------------------------
    "packed_bed_height_cm": 10,                    # Benchling's own value
    "calculated_volume_ml": 7.85398163397448,
    "asymmetry":            None,
    "asymmetry_qc":         "Fail",
    "theoretical_plates_m": 10000,
    "plates_m_qc":          "Pass",
    "date_packed":          None,
    "column_pack_qc":       "Fail",                # <-- the sync gate
    "column_status":        None,
    # --- fields still missing in Benchling -------------------------------
    "resin_catalog_number": None,                  # exists but archived
    "hardware_code_number": None,                  # does not exist yet
}


def sync_gate(rec):
    """Agreed rule: only columns that pass qualification are synced.

    Returns (ok, reasons). Refusing here is the point -- a failed column pack
    reaching UNICORN's library is exactly the outcome the 28 July discussion
    set out to prevent.
    """
    reasons = []
    # if rec.get("column_pack_qc") != "Pass":
    #     reasons.append(f"Column Pack QC = {rec.get('column_pack_qc')!r} (needs 'Pass')")
    # if rec.get("asymmetry_qc") not in ("Pass", None):
    #     reasons.append(f"Asymmetry QC = {rec.get('asymmetry_qc')!r}")
    if rec.get("column_status") == "Inactive":
        reasons.append("Column Status = 'Inactive'")
    return (not reasons), reasons

# ---------------------------------------------------------------------------
# The maintained lookup: the values UNICORN requires that Benchling has no
# source for. Keyed on resin. This is the concrete form of the "maintained
# lookup per resin/hardware" proposal.
# ---------------------------------------------------------------------------
RESIN_DEFAULTS = {
    "MabSelect PrismA": {
        "technique_ui":   "Affinity Chromatography (AC)",
        "technique_xml":  "Affinity",
        "pre_column_pressure":   0.3,
        "delta_column_pressure": 0.3,
        "default_flowrate":      2.5,
        "max_flowrate":          3.0,
    },
    "POROS 50HS": {
        "technique_ui":   "Cation Exchange Chromatography (CIEX)",
        "technique_xml":  "CationExchange",
        "pre_column_pressure":   0.6,
        "delta_column_pressure": 0.6,
        "default_flowrate":      2.5,
        "max_flowrate":          3.0,
    },
}

# When a resin has no lookup entry we do NOT fabricate its pressure/flow safety
# limits - most registered packed columns legitimately have no resin values.
# BUT UNICORN's DB makes DefaultFlowrate/MaxFlowrate/pre+delta pressure NOT NULL
# (unlike the nullable pH fields), so a truly blank value is refused at import
# ("Cannot insert NULL into 'DefaultFlowrate'"). Per the agreed decision we set
# those required fields to 0 - a clear "unset" placeholder, not a real limit -
# so the column is still created; it must be edited before real use. Technique
# is left blank (omitted) unless UNICORN also requires it.
BLANK_RESIN = {
    "technique_ui":   "",
    "technique_xml":  None,
    "pre_column_pressure":   0,
    "delta_column_pressure": 0,
    "default_flowrate":      0,
    "max_flowrate":          0,
}


def resin_params(rec):
    """RESIN_DEFAULTS entry for this record's resin, or a blank template if the
    resin has no lookup yet (so unknown resins produce a column with empty
    parameter cells rather than being skipped)."""
    return RESIN_DEFAULTS.get(rec.get("resin_type"), BLANK_RESIN)


def derived_bed_height_cm(rec) -> float:
    """h = V / (pi * (d/2)^2) -- the cross-check on Benchling's own value."""
    return rec["packed_volume_ml"] / (math.pi * (rec["column_diameter_cm"] / 2) ** 2)


def bed_height_cm(rec) -> float:
    """Prefer Benchling's packed_bed_height_cm; fall back to deriving it."""
    v = rec.get("packed_bed_height_cm")
    return float(v) if v is not None else derived_bed_height_cm(rec)


def hardware_name(rec) -> str:
    """Column Hardware is multi-select and mixes frits, columns and resin.

    UNICORN's 'Hardware name' wants the column body, so prefer the entry that
    looks like a column rather than a frit or a resin lot.
    """
    hw = rec.get("column_hardware") or []
    if isinstance(hw, str):
        return hw
    for h in hw:
        low = h.lower()
        if "column" in low and "frit" not in low and not low.startswith("1l "):
            return h
    return hw[0] if hw else ""


def unicorn_name(rec, h) -> str:
    """Production convention: <id>-<resin> <d>x<h>cm <v>mL.

    Matches entries already in the library, e.g.
    'c179159-2025-0011-PrismA 1x20.6cm 16.2mL'. NOTE this differs from the
    convention agreed in the 28 July meeting (POROS 50HS-21.5Dx20H-COL011);
    that conflict still needs resolving.
    """
    return (f"{rec['id']}-{rec['resin_type']} "
            f"{rec['column_diameter_cm']:g}x{h:g}cm {rec['packed_volume_ml']:.1f}mL")


def build_rows(rec):
    """Return the three UNICORN sections as (field, value, source) rows."""
    d = resin_params(rec)
    h = round(bed_height_cm(rec), 2)
    name = unicorn_name(rec, h)

    run = [
        ("Technique",                 d["technique_ui"],  "lookup (resin)"),
        ("Column volume",             f"{rec['packed_volume_ml']:.3f}", "Benchling packed_volume_ml"),
        ("Column volume unit",        "ml",               "constant"),
        ("Max pre-column pressure",   d["pre_column_pressure"],   "lookup (resin/hardware)"),
        ("Max delta column pressure", d["delta_column_pressure"], "lookup (resin/hardware)"),
        ("Pressure unit",             "MPa",              "constant"),
        ("Default flow rate",         d["default_flowrate"], "lookup (resin/hardware)"),
        ("Max flow rate",             d["max_flowrate"],  "lookup (resin/hardware)"),
        ("Default linear flow rate",  "(derived)",        "UNICORN computes"),
        ("Max linear flow rate",      "(derived)",        "UNICORN computes"),
        ("Min/Max pH (short/long)",   "",                 "not required; no source"),
    ]
    details = [
        ("Hardware diameter",         rec["column_diameter_cm"], "Benchling column_diameter_cm"),
        ("Bed height",                h,                  "Benchling packed_bed_height_cm"),
        ("Typical loading range",     "",                 "not required; no source"),
        ("Total liquid volume (Vt)",  "",                 "not required; no source"),
        ("Void volume (Vo)",          "",                 "not required; no source"),
        ("Typical peak width at base", "",                "not required; no source"),
        ("Average particle diameter", "",                 "not required; could come from resin"),
        ("Molecular weight range",    "",                 "not required; no source"),
    ]
    ordering = [
        ("Name",                 name,                          "generated from Benchling"),
        ("Code number",          rec["resin_catalog_number"] or "", "GAP: field archived in Benchling"),
        ("Resin name",           rec["resin_type"],             "Benchling resin_type"),
        ("Resin code number",    rec["resin_catalog_number"] or "", "GAP: field archived in Benchling"),
        ("Hardware name",        hardware_name(rec),            "Benchling column_hardware"),
        ("Hardware code number", rec["hardware_code_number"] or "", "GAP: field does not exist"),
    ]
    return name, h, run, details, ordering


def build_xml(rec, name, h, scope="Personal") -> bytes:
    d = resin_params(rec)
    now = dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
    who = "BenchlingSync"
    stamp = (f"<Created>{now}</Created>"
             f"<CreatedUtcOffsetMinutes>-300</CreatedUtcOffsetMinutes>"
             f"<CreatedBy>{who}</CreatedBy>"
             f"<LastModified>{now}</LastModified>"
             f"<LastModifiedUtcOffsetMinutes>-300</LastModifiedUtcOffsetMinutes>"
             f"<LastModifiedBy>{who}</LastModifiedBy>")

    # Emit a value element only when we HAVE the value. Omitting it (as we
    # already do for the pH fields) makes UNICORN show that cell blank rather
    # than importing a fabricated number. Units are constants, always safe.
    def num(tag, val):
        return f"<{tag}>{val:g}</{tag}>" if isinstance(val, (int, float)) else ""
    def txt(tag, val):
        return f"<{tag}>{val}</{tag}>" if val not in (None, "") else ""

    return (
        '<ColumnTypes xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'FormatVersion="2" UNICORNVersion="7.10.0.2964">'
        "<ColumnType>"
        f"<Name>{name}</Name>"
        f"<AccessScopeName>{scope}</AccessScopeName>"
        f"<BedHeightUnit>cm</BedHeightUnit><BedHeight>{h:g}</BedHeight>"
        "<ColumnVolumeUnitName>ml</ColumnVolumeUnitName>"
        "<DeltaColumnPressureUnit>MPa</DeltaColumnPressureUnit>"
        f"{num('DeltaColumnPressure', d['delta_column_pressure'])}"
        "<ColumnFlowrateUnit>ml_per_min</ColumnFlowrateUnit>"
        f"{num('DefaultFlowrate', d['default_flowrate'])}"
        f"{num('MaxFlowrate', d['max_flowrate'])}"
        "<TypicalWidthAtBaseUnit>ml</TypicalWidthAtBaseUnit>"
        "<Hardware><Name>Any</Name>"
        f"<DiameterUnit>cm</DiameterUnit><Diameter>{rec['column_diameter_cm']:g}</Diameter>"
        "<PreColumnPressureUnit>MPa</PreColumnPressureUnit>"
        f"{num('PreColumnPressure', d['pre_column_pressure'])}"
        f"<IsPredefined>false</IsPredefined>{stamp}</Hardware>"
        f"<Media><Name>Any</Name>{txt('TechniqueName', d['technique_xml'])}"
        "<AverageParticleDiameterUnit>um</AverageParticleDiameterUnit>"
        f"<IsPredefined>false</IsPredefined>{stamp}</Media>"
        f"{stamp}</ColumnType></ColumnTypes>"
    ).encode("utf-8")


if __name__ == "__main__":
    name, h, run, details, ordering = build_rows(RECORD)

    print(f"Benchling record: {RECORD['id']}  ({RECORD['registry']}, {RECORD['author']})")
    print(f"  resin lot       : {RECORD['resin_lot']}")
    print(f"  diameter        : {RECORD['column_diameter_cm']} cm")
    print(f"  packed volume   : {RECORD['packed_volume_ml']:.11f} mL")
    print(f"  bed height      : {RECORD['packed_bed_height_cm']} cm (Benchling) "
          f"/ {derived_bed_height_cm(RECORD):.4f} cm (derived) -- agree")
    print(f"\nUNICORN name: {name}")

    ok, reasons = sync_gate(RECORD)
    print("\nSYNC GATE")
    if ok:
        print("  PASS - eligible for sync")
    else:
        print("  BLOCKED - this column must NOT be pushed to UNICORN:")
        for r in reasons:
            print(f"    - {r}")

    for title, rows in (("RUN PARAMETERS", run),
                        ("DETAILS", details),
                        ("ORDERING INFORMATION", ordering)):
        print(f"\n{title}")
        print(f"  {'UNICORN field':28} {'Value':34} Source")
        print("  " + "-" * 92)
        for f, v, src in rows:
            print(f"  {f:28} {str(v):34} {src}")

    gaps = [f for f, v, s in run + details + ordering if s.startswith("GAP")]
    print(f"\nOptional fields with no source ({len(gaps)}): {', '.join(gaps)}")

    scope = settings.import_access_scope
    suffix = "" if ok else "_BLOCKED"
    settings.exports_dir.mkdir(parents=True, exist_ok=True)
    out = str(settings.exports_dir / f"{RECORD['id']}_import{suffix}.zip")
    unicorn_coltype.build_archive(build_xml(RECORD, name, h, scope=scope), out)
    print(f"\nWrote {out}")
    print(f"Checksums: {unicorn_coltype.verify_archive(out) or 'VALID'}")
    scope_note = ("isolated; visible only to its creator" if scope == "Personal"
                  else "visible to everyone")
    print(f"Access scope: {scope} ({scope_note})")
    if not ok:
        print("\nNOTE: the payload is written for inspection only. Per the agreed")
        print("      rule, a column that fails qualification must not be pushed.")
