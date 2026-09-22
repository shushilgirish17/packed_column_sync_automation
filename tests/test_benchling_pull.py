"""Unit tests for the producer's pure logic (tools/benchling_pull.py).

The Benchling fetch and file I/O in main() are integration concerns (need
creds / a filesystem queue) and are exercised via --mock at runtime, not here.
"""

import unicorn_coltype
import benchling_pull as bp
import generate_import as gi


# --------------------------------------------------------------------------
# A tiny fake Benchling client mimicking the SDK shapes the transport uses:
#   custom_entities.list(schema_id, sort, name) / get_by_id(id)
#   assay_results.list(schema_id, entity_ids, sort)
# Field objects expose .value / .display_value; Fields exposes .get / keys.
# --------------------------------------------------------------------------
class _F:
    def __init__(self, value, display=None):
        self.value = value
        self.display_value = display if display is not None else value


class _Fields:
    def __init__(self, d):
        # Wrap plain values as field objects (as the SDK does): each has .value.
        self._d = {k: (v if isinstance(v, _F) else _F(v)) for k, v in d.items()}
        self.additional_keys = list(d.keys())

    def get(self, name):
        return self._d.get(name)


class _Entity:
    def __init__(self, id, name, created_at, fields):
        self.id = id
        self.name = name
        self.entity_registry_id = name
        self.created_at = created_at
        self.fields = _Fields(fields)


class _Result:
    def __init__(self, id, created_at, entity_id, schema_id, fields):
        self.id = id
        self.created_at = created_at
        self._entity_id = entity_id
        self._schema_id = schema_id
        self.fields = _Fields(fields)


class _EntitySvc:
    def __init__(self, entities):
        self._e = entities

    def list(self, schema_id=None, sort=None, name=None, **_):
        items = [e for e in self._e
                 if name is None or e.name == name]
        if sort and "createdAt:desc" in sort:
            items = sorted(items, key=lambda e: e.created_at, reverse=True)
        return [items]  # one page

    def get_by_id(self, entity_id, **_):
        for e in self._e:
            if e.id == entity_id:
                return e
        raise KeyError(entity_id)


class _ResultSvc:
    def __init__(self, results):
        self._r = results

    def list(self, schema_id=None, entity_ids=None, sort=None, **_):
        items = [r for r in self._r
                 if (schema_id is None or r._schema_id == schema_id)
                 and (entity_ids is None or r._entity_id in entity_ids)]
        if sort and "createdAt:desc" in sort:
            items = sorted(items, key=lambda r: r.created_at, reverse=True)
        return [items]


class _FakeBench:
    def __init__(self, entities, results):
        self.custom_entities = _EntitySvc(entities)
        self.assay_results = _ResultSvc(results)


def test_mock_record_active():
    assert bp.mock_record(active=True)["column_status"] == "Active"


def test_mock_record_inactive():
    assert bp.mock_record(active=False)["column_status"] == "Inactive"


def test_mock_record_resin_is_known_to_lookup():
    # Otherwise build_xml would KeyError on RESIN_DEFAULTS.
    assert bp.mock_record(True)["resin_type"] in gi.RESIN_DEFAULTS


def test_parse_resin_from_lot():
    assert bp._parse_resin("1L MabSelect PrismA-17549803-001 RMLOT060") == "MabSelect PrismA"


def test_set_string_carries_the_writable_run_params():
    rec = bp.mock_record(active=True)
    _n, _h, run, _d, _o = gi.build_rows(rec)
    s = bp.set_string(run)
    assert "Default flow rate=" in s
    assert "Max flow rate=" in s
    assert "Max pre-column pressure=" in s
    # semicolon-joined pairs, the shape sync_column.ps1 -Set expects
    assert s.count(";") == 3


def test_gate_is_active_only():
    # The producer's contract: Active -> eligible, anything else -> skip.
    assert bp.mock_record(True)["column_status"] == "Active"
    assert bp.mock_record(False)["column_status"] != "Active"


# --------------------------------------------------------------------------
# The real-mode mapping: entity + result field dicts -> record -> valid zip.
# --------------------------------------------------------------------------
def _entity_fields():
    return {
        bp.E_DIAMETER:   "1.0",
        bp.E_RESIN_TYPE: "MabSelect PrismA",
        bp.E_RESIN_LOT:  "1L MabSelect PrismA-17549803-001 RMLOT060",
        bp.E_RESIN_CAT:  "17549803",
        bp.E_HARDWARE:   "bfi_hw1",
    }


def _packing_fields(status="Active"):
    return {bp.R_VOLUME: 7.854, bp.R_BED_HEIGHT: 10, bp.R_STATUS: status,
            bp.R_PACK_QC: "Pass", bp.R_ASYM_QC: "Pass"}


def test_map_to_record_shapes_the_record():
    rec = bp.map_to_record(
        registry_id="COL0123", entity_name="COL0123",
        entity_fields=_entity_fields(), packing_fields=_packing_fields(),
        hardware_name="Omnifit Glass Column", hardware_code="OMNI-150")
    assert rec["resin_type"] in gi.RESIN_DEFAULTS
    assert rec["column_diameter_cm"] == 1.0            # coerced from "1.0"
    assert rec["packed_volume_ml"] == 7.854            # from the result row
    assert rec["packed_bed_height_cm"] == 10
    assert rec["column_status"] == "Active"            # gate field from result
    assert rec["hardware_code_number"] == "OMNI-150"


def test_resin_name_canonicalised_by_spacing_and_case():
    # Benchling spells it 'POROS 50 HS'; the lookup key is 'POROS 50HS'.
    ef = _entity_fields()
    ef[bp.E_RESIN_TYPE] = "POROS 50 HS"
    ef[bp.E_RESIN_LOT] = "1L POROS 50 HS-123"
    rec = bp.map_to_record(registry_id="COL010", entity_name="COL010",
                           entity_fields=ef, packing_fields=_packing_fields())
    assert rec["resin_type"] == "POROS 50HS"
    assert rec["resin_type"] in gi.RESIN_DEFAULTS


def test_map_to_record_parses_resin_when_type_not_a_known_key():
    ef = _entity_fields()
    ef[bp.E_RESIN_TYPE] = "Some Unmapped Label"        # not in RESIN_DEFAULTS
    rec = bp.map_to_record(registry_id="X", entity_name="X",
                           entity_fields=ef, packing_fields=_packing_fields())
    assert rec["resin_type"] == "MabSelect PrismA"     # parsed from the lot


def test_mapped_record_passes_gate_and_builds_valid_zip(tmp_path):
    rec = bp.map_to_record(
        registry_id="COL0123", entity_name="COL0123",
        entity_fields=_entity_fields(), packing_fields=_packing_fields())
    ok, reasons = gi.sync_gate(rec)
    assert ok, reasons
    name, h, run, _d, _o = gi.build_rows(rec)
    zip_path = tmp_path / "out.zip"
    unicorn_coltype.build_archive(gi.build_xml(rec, name, h), str(zip_path))
    assert unicorn_coltype.verify_archive(str(zip_path)) == []      # [] == VALID


def test_inactive_result_blocks_the_gate():
    rec = bp.map_to_record(registry_id="X", entity_name="X",
                           entity_fields=_entity_fields(),
                           packing_fields=_packing_fields(status="Inactive"))
    assert rec["column_status"] == "Inactive"          # producer's --id path skips


# --------------------------------------------------------------------------
# The transport: fetch_from_benchling(latest=True) against a fake SDK client.
# Verifies newest-entity selection + the result join, with no creds/SDK.
# --------------------------------------------------------------------------
def test_fetch_latest_selects_newest_entity_and_joins_result(monkeypatch):
    from config import settings
    older = _Entity("etr_1", "COL0001", "2026-01-01T00:00:00Z", _entity_fields())
    newer = _Entity("etr_2", "COL0002", "2026-09-01T00:00:00Z", _entity_fields())
    hardware = _Entity("bfi_hw1", "Omnifit Benchmark Glass Column",
                       "2020-01-01T00:00:00Z", {"Catalog #": _F("OMNI-150")})
    packing = _Result("res_new", "2026-09-02T00:00:00Z", "etr_2",
                      settings.benchling_packing_schema, _packing_fields())
    stale   = _Result("res_old", "2026-08-01T00:00:00Z", "etr_2",
                      settings.benchling_packing_schema,
                      _packing_fields(status="Inactive"))
    fake = _FakeBench([older, newer, hardware], [stale, packing])
    monkeypatch.setattr(bp, "_make_client", lambda: fake)

    rec = bp.fetch_from_benchling(latest=True)
    assert rec["id"] == "COL0002"                      # newest entity chosen
    assert rec["column_status"] == "Active"            # newest result row joined
    assert rec["hardware_code_number"] == "OMNI-150"   # link traversed
    ok, reasons = gi.sync_gate(rec)
    assert ok, reasons
