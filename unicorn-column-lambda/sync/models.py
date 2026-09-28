"""Plain data structures shared by the sync modules (no Benchling SDK dependency)."""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class SyncError(Exception):
    """Base class. `kind` matches the Exceptions table in the design doc."""
    kind = "Sync Error"


class ValidationError(SyncError):
    kind = "Validation Error"


class MappingError(SyncError):
    kind = "Mapping Error"


class AuthenticationError(SyncError):
    kind = "Authentication Error"


class BenchlingUnavailableError(SyncError):
    kind = "Benchling API Unavailable"


class FileWriteError(SyncError):
    kind = "File Write Error"


class WebhookError(SyncError):
    kind = "Webhook Processing Error"


class FieldBag:
    """Normalised view of Benchling `fields`: name -> (value, display_value)."""

    def __init__(self, values: Optional[Dict[str, Dict[str, Any]]] = None):
        self._values = dict(values or {})

    @classmethod
    def from_sdk(cls, fields) -> "FieldBag":
        if fields is None:
            return cls()
        out = {}
        for key in fields.additional_keys:
            f = fields[key]
            out[key] = {"value": getattr(f, "value", None), "display": getattr(f, "display_value", None)}
        return cls(out)

    def merged_with(self, other: "FieldBag") -> "FieldBag":
        """Return a bag where self wins and `other` only fills gaps."""
        merged = dict(other._values)
        merged.update({k: v for k, v in self._values.items() if not _is_empty(v.get("value"))})
        return FieldBag(merged)

    def _find(self, candidates: List[str]):
        """First candidate present with a value. Exact name first, then case-insensitive
        (tenants differ, e.g. 'Resin Lot' vs 'resin lot')."""
        lowered = {k.strip().lower(): k for k in self._values}
        for name in candidates:
            key = name if name in self._values else lowered.get(name.strip().lower())
            if key is not None and not _is_empty(self._values[key].get("value")):
                return key, self._values[key]
        return None, None

    def value(self, candidates: List[str]):
        return (self._find(candidates)[1] or {}).get("value")

    def display(self, candidates: List[str]):
        entry = self._find(candidates)[1] or {}
        return entry.get("display") if not _is_empty(entry.get("display")) else entry.get("value")

    def source(self, candidates: List[str]) -> Optional[str]:
        return self._find(candidates)[0]


def _is_empty(v):
    return v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, list) and not v)


@dataclass
class LinkedRecord:
    id: str
    name: Optional[str]
    fields: FieldBag = field(default_factory=FieldBag)


@dataclass
class ColumnSource:
    """Everything read from Benchling for one packed column."""
    id: str
    name: str
    registry_id: Optional[str]
    web_url: Optional[str]
    fields: FieldBag
    resin_library: Optional[LinkedRecord] = None   # Resin Characteristics Library result row
    resin_material: Optional[LinkedRecord] = None  # SAP Raw Material linked from the library row
    hardware: Optional[LinkedRecord] = None        # Column Hardware entity
    resolution_notes: List[str] = field(default_factory=list)


@dataclass
class ColumnType:
    """Mapped UNICORN Column Type, grouped as in the Column Handling dialog."""
    name: str
    status: str
    parameters: Dict[str, Dict[str, Dict[str, Any]]]  # section -> parameter -> {"value", "unit"}
    source: Dict[str, Any]
    unicorn: Dict[str, Any] = field(default_factory=dict)  # values for ColumnType.xml
    warnings: List[str] = field(default_factory=list)


@dataclass
class ColumnOutcome:
    column_id: str
    column_name: Optional[str] = None
    registry_id: Optional[str] = None
    result: str = "Failure"                # Queued / Skipped / Failure
    message: str = ""
    job_ref: Optional[str] = None          # SQS message id (or local job path)
    content_hash: Optional[str] = None
    column_type_name: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    error_kind: Optional[str] = None
