"""
unicorn_coltype.py
------------------
Build and verify UNICORN "Column Type" export/import archives.

Format (reverse-engineered from real exports, UNICORN 7.10.0.2964,
FormatVersion 2). A column type archive is a plain ZIP containing:

  ColumnType.xml   UTF-8, no BOM. <ColumnTypes> may hold MANY <ColumnType>
                   elements -- so one archive can carry a whole batch.
  Manifest.xml     integrity envelope, two CRC-32 values:

    <Details><CRCCode>   CRC-32 of ColumnType.xml's raw bytes.
    <ExportManifest CRCCode="">
                         CRC-32 of the manifest XML *with the CRCCode
                         attribute itself removed*.

Both are CRC-32 stored in a signed Int32 and rendered as Int64 hex, so a
negative value appears sign-extended: 0xB51CA80E -> "FFFFFFFFB51CA80E".
Verified against two independent exports.

This means import files can be generated directly -- no UI form-filling of
the nine required fields. The UI step reduces to Import -> pick file.

NOTE: generating a file is not the same as UNICORN accepting it. Round-trip
import must be validated on a TEST system, never against the shared
production database.
"""

from __future__ import annotations

import binascii
import io
import zipfile

PAYLOAD_NAME = "ColumnType.xml"
FILE_TYPE = "Column"

_MANIFEST_HEAD = (
    '<ExportManifest xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'FormatVersion="1"'
)


def crc_code(data: bytes) -> str:
    """CRC-32 of `data` in UNICORN's sign-extended Int64 hex form."""
    v = binascii.crc32(data) & 0xFFFFFFFF
    # Stored as Int32 then formatted as Int64: negatives sign-extend.
    return f"FFFFFFFF{v:08X}" if v & 0x80000000 else f"{v:08X}"


def build_manifest(payload: bytes, payload_name: str = PAYLOAD_NAME,
                   file_type: str = FILE_TYPE) -> bytes:
    """Build Manifest.xml for `payload`, with both CRCs correct."""
    details = (
        f"<Details><FileName>{payload_name}</FileName>"
        f"<CRCCode>{crc_code(payload)}</CRCCode>"
        f"<FileType>{file_type}</FileType></Details>"
    )
    # The envelope CRC is taken over the manifest WITHOUT its own attribute.
    without_attr = f"{_MANIFEST_HEAD}>{details}</ExportManifest>"
    envelope = crc_code(without_attr.encode("utf-8"))
    return f'{_MANIFEST_HEAD} CRCCode="{envelope}">{details}</ExportManifest>'.encode("utf-8")


def build_archive(column_xml: bytes, out_path: str) -> str:
    """Write a UNICORN-importable column type archive."""
    manifest = build_manifest(column_xml)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(PAYLOAD_NAME, column_xml)
        z.writestr("Manifest.xml", manifest)
    return out_path


def verify_archive(path: str) -> list[str]:
    """Check an archive's CRCs. Returns a list of problems ([] == valid)."""
    import re

    problems: list[str] = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        for required in (PAYLOAD_NAME, "Manifest.xml"):
            if required not in names:
                problems.append(f"missing {required}")
        if problems:
            return problems
        payload = z.read(PAYLOAD_NAME)
        manifest = z.read("Manifest.xml").decode("utf-8")

    m = re.search(r"<CRCCode>([0-9A-F]+)</CRCCode>", manifest)
    expected = crc_code(payload)
    if not m:
        problems.append("no per-file CRCCode in manifest")
    elif m.group(1) != expected:
        problems.append(f"per-file CRC {m.group(1)} != computed {expected}")

    a = re.search(r'\sCRCCode="([0-9A-F]+)"', manifest)
    stripped = re.sub(r'\sCRCCode="[0-9A-F]+"', "", manifest, count=1)
    env_expected = crc_code(stripped.encode("utf-8"))
    if not a:
        problems.append("no envelope CRCCode attribute")
    elif a.group(1) != env_expected:
        problems.append(f"envelope CRC {a.group(1)} != computed {env_expected}")

    return problems


if __name__ == "__main__":
    import pathlib
    import sys

    # Self-test: our generated manifest must be byte-identical to real UNICORN
    # exports. Defaults to the sample exports in ../fixtures.
    _fixtures = pathlib.Path(__file__).resolve().parent.parent / "fixtures"
    samples = sys.argv[1:] or [
        str(_fixtures / "export_sample_a.zip"),
        str(_fixtures / "export_sample_b.zip"),
    ]
    for path in samples:
        with zipfile.ZipFile(path) as z:
            payload = z.read(PAYLOAD_NAME)
            real_manifest = z.read("Manifest.xml")
        ours = build_manifest(payload)
        same = ours == real_manifest
        print(pathlib.Path(path).name)
        print(f"  manifest byte-identical to UNICORN's : {same}")
        if not same:
            print(f"    real: {real_manifest.decode()}")
            print(f"    ours: {ours.decode()}")
        print(f"  verify_archive()                     : "
              f"{verify_archive(path) or 'VALID'}")
