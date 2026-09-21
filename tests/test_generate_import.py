"""Unit tests for the Benchling->UNICORN mapping (tools/generate_import.py)."""

import generate_import as gi
import unicorn_coltype as uc


def base_rec(**over):
    """A valid Active POROS 50HS record (resin is in RESIN_DEFAULTS)."""
    r = {
        "id": "COL999",
        "resin_type": "POROS 50HS",
        "column_diameter_cm": 2.5,
        "packed_volume_ml": 95.0,
        "packed_bed_height_cm": 19.4,
        "column_hardware": ["10 um PE Frit", "2.5cm Glass Column", "1L Resin lot"],
        "resin_catalog_number": None,
        "hardware_code_number": None,
        "asymmetry_qc": "Pass",
        "plates_m_qc": "Pass",
        "column_pack_qc": "Pass",
        "column_status": "Active",
    }
    r.update(over)
    return r


# --- the qualification gate ------------------------------------------------
def test_gate_passes_for_qualified_active():
    ok, reasons = gi.sync_gate(base_rec())
    assert ok and reasons == []


def test_gate_blocks_failed_qc():
    ok, reasons = gi.sync_gate(base_rec(column_pack_qc="Fail"))
    assert not ok and any("Column Pack QC" in r for r in reasons)


def test_gate_blocks_inactive():
    ok, reasons = gi.sync_gate(base_rec(column_status="Inactive"))
    assert not ok


# --- bed height ------------------------------------------------------------
def test_bed_height_prefers_benchling_value():
    assert gi.bed_height_cm(base_rec(packed_bed_height_cm=20)) == 20.0


def test_bed_height_derived_from_volume_when_missing():
    r = base_rec(packed_bed_height_cm=None, column_diameter_cm=1.0,
                 packed_volume_ml=7.85398163397448)
    assert round(gi.bed_height_cm(r), 2) == 10.0


# --- misc mapping ----------------------------------------------------------
def test_hardware_name_prefers_the_column_body():
    assert "Column" in gi.hardware_name(base_rec())


def test_unicorn_name_convention():
    name = gi.unicorn_name(base_rec(), 19.4)
    assert name.startswith("COL999-POROS 50HS")
    assert "2.5x19.4cm" in name


def test_build_rows_has_all_required_fields():
    _n, _h, run, details, _ordering = gi.build_rows(base_rec())
    labels = [f for f, _v, _s in run + details]
    for required in ("Technique", "Max pre-column pressure", "Max delta column pressure",
                     "Default flow rate", "Max flow rate", "Hardware diameter", "Bed height"):
        assert required in labels


# --- build_xml -------------------------------------------------------------
def test_build_xml_uses_the_passed_records_diameter():
    # Regression: build_xml once hardcoded the module-global RECORD (diameter
    # 1.0) for the Diameter element. It must reflect the record passed in.
    r = base_rec(column_diameter_cm=2.5)
    name, h, *_ = gi.build_rows(r)
    xml = gi.build_xml(r, name, h).decode("utf-8")
    assert "<Diameter>2.5</Diameter>" in xml


def test_build_xml_produces_a_valid_archive(tmp_path):
    r = base_rec()
    name, h, *_ = gi.build_rows(r)
    out = tmp_path / "g.zip"
    uc.build_archive(gi.build_xml(r, name, h), str(out))
    assert uc.verify_archive(str(out)) == []


def test_build_xml_scope_is_honoured():
    r = base_rec()
    name, h, *_ = gi.build_rows(r)
    xml = gi.build_xml(r, name, h, scope="Personal").decode("utf-8")
    assert "<AccessScopeName>Personal</AccessScopeName>" in xml
