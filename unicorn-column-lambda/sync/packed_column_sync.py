"""packed-column-sync: orchestrates one synchronisation run (design doc steps 3-9).

Audit trail = one structured JSON line per column in CloudWatch Logs ("audit": {...}).
Search in CloudWatch Logs Insights with:  filter audit.result = "Failure"
"""
import json
import logging
from typing import List

import config
from sync.benchling_reader import BenchlingReader
from sync.import_file import content_hash, queue_import_job
from sync.mapping import map_column
from sync.models import ColumnOutcome, SyncError
from sync.validate import validate_column

log = logging.getLogger(__name__)


def sync_columns(reader: BenchlingReader, column_ids: List[str], trigger: dict) -> List[ColumnOutcome]:
    """
    trigger: {"type": "canvas"|"schedule"|"manual", "user": ..., "entry_id": ..., "canvas_id": ...}
    Never raises for per-column problems; each column gets its own outcome.
    Only Active columns are queued; inactive ones never reach UNICORN.
    """
    outcomes = []
    reader.prefetch(column_ids)

    for column_id in dict.fromkeys(column_ids):  # de-dupe, keep order
        outcome = ColumnOutcome(column_id=column_id)
        try:
            src = reader.load_column(column_id)
            outcome.column_name, outcome.registry_id = src.name, src.registry_id

            validate_column(src)
            ct = map_column(src)
            outcome.column_type_name, outcome.warnings = ct.name, ct.warnings
            outcome.content_hash = content_hash(ct)

            if ct.status.strip().lower() != config.ACTIVE_STATUS.lower():
                outcome.result = "Skipped"
                outcome.message = f"Column Status is {ct.status} - not sent to UNICORN"
            else:
                outcome.job_ref = queue_import_job(ct, outcome.content_hash)
                outcome.result = "Queued"
                outcome.message = "Queued for UNICORN (import if new, update if it exists)"
        except SyncError as e:
            outcome.error_kind, outcome.message = e.kind, str(e)
        except Exception as e:  # unexpected - keep the batch going
            outcome.error_kind, outcome.message = "Unexpected Error", f"{type(e).__name__}: {e}"
            log.exception("Column %s failed unexpectedly", column_id)

        _audit(outcome, trigger)
        outcomes.append(outcome)
    return outcomes


def _audit(o: ColumnOutcome, trigger: dict):
    record = {
        "benchling_entity_id": o.column_id,
        "benchling_registry_id": o.registry_id,
        "column_type_name": o.column_type_name,
        "result": o.result,
        "error_kind": o.error_kind,
        "message": o.message,
        "job_ref": o.job_ref,
        "content_hash": o.content_hash,
        "warnings": o.warnings,
        "trigger": trigger.get("type"),
        "user": trigger.get("user"),
        "entry_id": trigger.get("entry_id"),
    }
    line = json.dumps({"audit": {k: v for k, v in record.items() if v not in (None, "", [])}}, default=str)
    (log.error if o.result == "Failure" else log.info)(line)
