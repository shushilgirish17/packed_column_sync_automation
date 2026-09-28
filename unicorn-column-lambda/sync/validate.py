"""validate-packed-column: mandatory field checks (design doc, 'Mandatory Fields')."""
import re
from typing import Optional

import config
from sync.models import ColumnSource, ValidationError

_NUMBER = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def to_number(value) -> Optional[float]:
    """Accept Benchling float/int fields and text like '1.0 cm'. Returns None if not numeric."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _NUMBER.search(str(value).replace(",", ""))
    return float(match.group()) if match else None


def validate_column(src: ColumnSource) -> None:
    f = config.FIELDS
    problems = []

    if not src.id:
        problems.append("Packed Column ID")
    if not src.name:
        problems.append("Packed Column Name")

    for key, label in (("diameter", "Column Diameter"), ("bed_height", "Bed Height"), ("volume", "Column Volume")):
        raw = src.fields.value(f[key])
        num = to_number(raw)
        if raw is None:
            problems.append(label)
        elif num is None or num <= 0:
            problems.append(f"{label} (not a positive number: {raw!r})")

    if src.resin_library is None:
        problems.append("Resin reference (no Resin Characteristics Library row resolved"
                        + (f": {'; '.join(src.resolution_notes)}" if src.resolution_notes else "") + ")")

    if not src.fields.display(f["status"]):
        problems.append("Status")

    if config.REQUIRE_PACK_QC_PASS:
        qc = src.fields.display(f["pack_qc"])
        if str(qc or "").strip().lower() != "pass":
            problems.append(f"Column Pack QC is {qc!r} (needs 'Pass')")

    if problems:
        raise ValidationError("Missing or invalid mandatory fields: " + ", ".join(problems))
