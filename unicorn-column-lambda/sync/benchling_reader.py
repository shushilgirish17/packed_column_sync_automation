"""
Benchling reads: packed columns, linked hardware, and resolve-resin-characteristics.

All Benchling access goes through `_call` so tenant IP-allowlist rejections (HTML page
instead of JSON) and auth failures surface as the typed errors from the design doc.
"""
import logging
from typing import Iterable, List, Optional

import httpx
from benchling_api_client.v2.extensions import NotPresentError
from benchling_sdk.errors import BenchlingError

import config
from sync.models import (AuthenticationError, BenchlingUnavailableError, ColumnSource, FieldBag,
                         LinkedRecord)

log = logging.getLogger(__name__)


def _flatten(page_iterator) -> list:
    return [item for page in page_iterator for item in page]


def _ids(value) -> List[str]:
    """Entity-link values: an id or a list of ids. Only custom-entity ids (bfi_) are kept, so a
    dropdown option id (sfso_) or free text in a same-named field is never used as a link."""
    values = value if isinstance(value, list) else [value]
    return [v for v in values if isinstance(v, str) and v.startswith("bfi_")]


def _first_id(value) -> Optional[str]:
    ids = _ids(value)
    return ids[0] if ids else None


def _is_column_body(name: str) -> bool:
    """Column Hardware is multi-select and mixes frits, column bodies and resin lots;
    UNICORN's Hardware name wants the column body."""
    low = (name or "").lower()
    return "column" in low and "frit" not in low and not low.startswith("1l ")


def _attr(obj, name):
    """SDK models raise NotPresentError for unset attributes instead of returning None."""
    try:
        return getattr(obj, name)
    except NotPresentError:
        return None


def _origin_entry_id(entity):
    origin = _attr(entity, "registration_origin")
    return _attr(origin, "origin_entry_id") if origin else None


def _latest(results):
    return max(results, key=lambda r: r.created_at) if results else None


class BenchlingReader:
    def __init__(self, benchling):
        self.benchling = benchling
        self._entity_cache = {}
        self._library_by_item_code = None

    # ------------------------------------------------------------------ errors
    @staticmethod
    def _call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except BenchlingError as e:
            body = (e.content or b"")[:200].decode("utf-8", "replace").lstrip()
            if body.startswith("<"):
                raise AuthenticationError(
                    f"Benchling returned an HTML page (HTTP {e.status_code}) - source address is "
                    f"likely rejected by the tenant IP allowlist") from e
            if e.status_code in (401, 403):
                raise AuthenticationError(f"Benchling rejected credentials/permissions (HTTP {e.status_code})") from e
            if e.status_code == 429 or e.status_code >= 500:
                raise BenchlingUnavailableError(f"Benchling API unavailable after retries (HTTP {e.status_code})") from e
            raise
        except (httpx.TransportError, httpx.TimeoutException) as e:
            raise BenchlingUnavailableError(f"Benchling API unreachable: {e}") from e

    # ----------------------------------------------------------- column lookup
    def list_column_ids_in_entry(self, entry_id: str) -> List[str]:
        """Packed Columns registered from the given notebook entry."""
        entities = self._call(lambda: _flatten(
            self.benchling.custom_entities.list(schema_id=config.PACKED_COLUMN_SCHEMA_ID, page_size=100)))
        return [e.id for e in entities if _origin_entry_id(e) == entry_id]

    def list_all_column_ids(self) -> List[str]:
        """Every non-archived Packed Column (local tooling)."""
        entities = self._call(lambda: _flatten(
            self.benchling.custom_entities.list(schema_id=config.PACKED_COLUMN_SCHEMA_ID, page_size=100)))
        return [e.id for e in entities]

    def get_entity(self, entity_id: str):
        if entity_id not in self._entity_cache:
            self._entity_cache[entity_id] = self._call(self.benchling.custom_entities.get_by_id, entity_id)
        return self._entity_cache[entity_id]

    def _linked(self, entity_id: Optional[str]) -> Optional[LinkedRecord]:
        if not entity_id:
            return None
        entity = self.get_entity(entity_id)
        return LinkedRecord(id=entity.id, name=_attr(entity, "name"), fields=FieldBag.from_sdk(_attr(entity, "fields")))

    # ------------------------------------------------------------ full source
    def load_column(self, column_id: str) -> ColumnSource:
        entity = self.get_entity(column_id)
        fields = FieldBag.from_sdk(_attr(entity, "fields"))
        notes = []

        if config.COLUMN_PACKING_SCHEMA_ID:
            packing = _latest(self._call(lambda: _flatten(self.benchling.assay_results.list(
                schema_id=config.COLUMN_PACKING_SCHEMA_ID, entity_ids=[column_id]))))
            if packing is not None:
                fields = fields.merged_with(FieldBag.from_sdk(packing.fields))
            else:
                notes.append("No Column Packing result row found; using Packed Column fields only")

        src = ColumnSource(
            id=entity.id,
            name=_attr(entity, "name"),
            registry_id=_attr(entity, "entity_registry_id") or None,
            web_url=_attr(entity, "web_url") or None,
            fields=fields,
            resolution_notes=notes,
        )
        self._resolve_resin(src)

        hardware = [self._linked(i) for i in _ids(fields.value(config.FIELDS["hardware_link"]))]
        if hardware:
            src.hardware = next((h for h in hardware if _is_column_body(h.name)), hardware[0])
            if len(hardware) > 1:
                src.resolution_notes.append(f"Column Hardware has {len(hardware)} links; used '{src.hardware.name}'")
        return src

    # ------------------------------------------- resolve-resin-characteristics
    def _resolve_resin(self, src: ColumnSource) -> None:
        f = config.FIELDS
        resin_id = _first_id(src.fields.value(f["resin_link"]))
        row = None

        if resin_id:
            rows = self._call(lambda: _flatten(self.benchling.assay_results.list(
                schema_id=config.RESIN_LIBRARY_SCHEMA_ID, entity_ids=[resin_id])))
            # entity_ids matches any link field; keep rows whose resin link is this resin
            rows = [r for r in rows if _first_id(FieldBag.from_sdk(r.fields).value(f["lib_resin_link"])) == resin_id] or rows
            if len(rows) > 1:
                src.resolution_notes.append(f"{len(rows)} Resin Characteristics Library rows for resin; used the latest")
            row = _latest(rows)
            if row is None:
                src.resolution_notes.append(f"resin {resin_id} has no Resin Characteristics Library row")

        if row is None:
            # Library rows that link the packed column itself (as tried in the on-prem demo)
            rows = self._call(lambda: _flatten(self.benchling.assay_results.list(
                schema_id=config.RESIN_LIBRARY_SCHEMA_ID, entity_ids=[src.id])))
            row = _latest(rows)
            if row is not None:
                src.resolution_notes.append("Resin library row found linked to the packed column itself")

        if row is None:
            catalog = src.fields.display(f["resin_catalog_number"])
            if catalog:
                row = self._library_index().get(str(catalog).strip().lower())
                if row is not None:
                    src.resolution_notes.append(f"Resin resolved by catalog number {catalog}")
                else:
                    src.resolution_notes.append(f"no library row whose resin Item Code is {catalog}")
            elif not resin_id:
                src.resolution_notes.append("column has neither a resin link nor a Resin Catalog Number")

        if row is None:
            return
        lib_fields = FieldBag.from_sdk(row.fields)
        src.resin_library = LinkedRecord(id=row.id, name=None, fields=lib_fields)
        src.resin_material = self._linked(_first_id(lib_fields.value(f["lib_resin_link"])))

    def _library_index(self):
        """Fallback: item code of each library row's linked raw material -> latest library row."""
        if self._library_by_item_code is None:
            f = config.FIELDS
            rows = self._call(lambda: _flatten(self.benchling.assay_results.list(
                schema_id=config.RESIN_LIBRARY_SCHEMA_ID, page_size=100)))
            index = {}
            for r in sorted(rows, key=lambda r: r.created_at):
                material = self._linked(_first_id(FieldBag.from_sdk(r.fields).value(f["lib_resin_link"])))
                code = material.fields.display(f["material_item_code"]) if material else None
                if code:
                    index[str(code).strip().lower()] = r
            self._library_by_item_code = index
        return self._library_by_item_code

    def prefetch(self, column_ids: Iterable[str]) -> None:
        ids = [i for i in column_ids if i not in self._entity_cache]
        for start in range(0, len(ids), 100):
            for e in self._call(self.benchling.custom_entities.bulk_get, ids[start:start + 100]) or []:
                self._entity_cache[e.id] = e
