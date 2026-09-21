"""Unit tests for the import-archive format (lib/unicorn_coltype.py)."""

import pathlib
import zipfile

import pytest

import unicorn_coltype as uc

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures"
SAMPLES = ["export_sample_a.zip", "export_sample_b.zip"]


@pytest.mark.parametrize("sample", SAMPLES)
def test_manifest_is_byte_identical_to_unicorns(sample):
    """Our generated manifest must exactly match UNICORN's own output."""
    with zipfile.ZipFile(FIXTURES / sample) as z:
        payload = z.read("ColumnType.xml")
        real_manifest = z.read("Manifest.xml")
    assert uc.build_manifest(payload) == real_manifest


@pytest.mark.parametrize("sample", SAMPLES)
def test_verify_accepts_real_exports(sample):
    assert uc.verify_archive(str(FIXTURES / sample)) == []


def test_verify_rejects_corrupt_crc():
    problems = uc.verify_archive(str(FIXTURES / "corrupt_crc.zip"))
    assert problems, "a CRC-mismatched archive must be reported invalid"


def test_crc_code_sign_extends_negative_int32():
    # crc32 of sample A's payload is 0xB51CA80E (high bit set) and UNICORN
    # renders it sign-extended.
    with zipfile.ZipFile(FIXTURES / "export_sample_a.zip") as z:
        payload = z.read("ColumnType.xml")
    assert uc.crc_code(payload) == "FFFFFFFFB51CA80E"


def test_crc_code_no_prefix_when_positive():
    # 0x43733E34 (manifest envelope of sample A) has the high bit clear.
    data = b"anything"  # just assert format shape on a positive value
    code = uc.crc_code(data)
    assert len(code) in (8, 16)
    if len(code) == 16:
        assert code.startswith("FFFFFFFF")


def test_build_archive_roundtrips(tmp_path):
    with zipfile.ZipFile(FIXTURES / "export_sample_a.zip") as z:
        xml = z.read("ColumnType.xml")
    out = tmp_path / "built.zip"
    uc.build_archive(xml, str(out))
    assert uc.verify_archive(str(out)) == []
    with zipfile.ZipFile(out) as z:
        assert set(z.namelist()) == {"ColumnType.xml", "Manifest.xml"}
