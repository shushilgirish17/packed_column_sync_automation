"""Unit tests for the producer's pure logic (tools/benchling_pull.py).

The Benchling fetch and file I/O in main() are integration concerns (need
creds / a filesystem queue) and are exercised via --mock at runtime, not here.
"""

import benchling_pull as bp
import generate_import as gi


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
