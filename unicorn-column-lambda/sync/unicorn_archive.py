"""
UNICORN Column Type import archive (UNICORN 7.10, FormatVersion 2).

Ported from unicorn_automation_test/lib/unicorn_coltype.py, where the format was
reverse-engineered from real exports and the manifest proven byte-identical.

A plain ZIP containing:
  ColumnType.xml  UTF-8, no BOM. <ColumnTypes> may hold many <ColumnType>.
  Manifest.xml    two CRC-32s that UNICORN enforces:
                    <Details><CRCCode>          CRC-32 of ColumnType.xml bytes
                    <ExportManifest CRCCode=""> CRC-32 of the manifest with that
                                                attribute removed
                  CRC stored as signed Int32 rendered as Int64 hex, so negatives
                  sign-extend: 0xB51CA80E -> "FFFFFFFFB51CA80E".
"""
import binascii
import io
import re
import zipfile
from typing import List

PAYLOAD_NAME = "ColumnType.xml"
MANIFEST_NAME = "Manifest.xml"
FILE_TYPE = "Column"

_MANIFEST_HEAD = ('<ExportManifest xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
                  'FormatVersion="1"')


def crc_code(data: bytes) -> str:
    v = binascii.crc32(data) & 0xFFFFFFFF
    return f"FFFFFFFF{v:08X}" if v & 0x80000000 else f"{v:08X}"


def build_manifest(payload: bytes) -> bytes:
    details = (f"<Details><FileName>{PAYLOAD_NAME}</FileName>"
               f"<CRCCode>{crc_code(payload)}</CRCCode>"
               f"<FileType>{FILE_TYPE}</FileType></Details>")
    envelope = crc_code(f"{_MANIFEST_HEAD}>{details}</ExportManifest>".encode("utf-8"))
    return f'{_MANIFEST_HEAD} CRCCode="{envelope}">{details}</ExportManifest>'.encode("utf-8")


def build_archive_bytes(column_xml: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(PAYLOAD_NAME, column_xml)
        z.writestr(MANIFEST_NAME, build_manifest(column_xml))
    return buf.getvalue()


def verify_archive_bytes(data: bytes) -> List[str]:
    """Problems found ([] == valid, i.e. UNICORN's checksums will match)."""
    problems = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        missing = [n for n in (PAYLOAD_NAME, MANIFEST_NAME) if n not in names]
        if missing:
            return [f"missing {n}" for n in missing]
        payload = z.read(PAYLOAD_NAME)
        manifest = z.read(MANIFEST_NAME).decode("utf-8")

    m = re.search(r"<CRCCode>([0-9A-F]+)</CRCCode>", manifest)
    if not m:
        problems.append("no per-file CRCCode in manifest")
    elif m.group(1) != crc_code(payload):
        problems.append(f"per-file CRC {m.group(1)} != computed {crc_code(payload)}")

    a = re.search(r'\sCRCCode="([0-9A-F]+)"', manifest)
    stripped = re.sub(r'\sCRCCode="[0-9A-F]+"', "", manifest, count=1)
    expected = crc_code(stripped.encode("utf-8"))
    if not a:
        problems.append("no envelope CRCCode attribute")
    elif a.group(1) != expected:
        problems.append(f"envelope CRC {a.group(1)} != computed {expected}")
    return problems
