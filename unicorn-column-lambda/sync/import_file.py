"""
generate-import-file: build the UNICORN Column Type import archive and hand it to the on-prem
UNICORN side (unicorn_automation_test: sync_column.ps1).

Per Active column:
    S3   <IMPORT_PREFIX>/<stem>.zip        UNICORN import archive (ColumnType.xml + Manifest.xml)
    S3   <IMPORT_PREFIX>/<stem>.job.json   full job record (contract + mapping + Benchling source)
    SQS  one message: columnName, set, zip, source, scope, generatedAt (the job contract
         sync_column.ps1 uses), contentHash, placeholders, warnings, s3 {bucket, zipKey, jobKey}
The consumer downloads s3.zipKey, then searches UNICORN for columnName:
missing -> Import the zip; present -> Edit the "set" values.
Without IMPORT_BUCKET (local runs) the zip + job.json go to LOCAL_OUTPUT_DIR/incoming instead.
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from xml.sax.saxutils import escape

import boto3

import config
from sync.models import ColumnType, FileWriteError
from sync.unicorn_archive import build_archive_bytes, verify_archive_bytes

# Run-parameter labels sync_column.ps1 can Edit on an existing column type, and where they come from.
_EDITABLE = (
    ("Max pre-column pressure", "pre_pressure"),
    ("Max delta column pressure", "delta_pressure"),
    ("Default flow rate", "default_flow"),
    ("Max flow rate", "max_flow"),
)


def content_hash(ct: ColumnType) -> str:
    """Stable hash of what UNICORN would receive (+ status). SQS FIFO deduplication id."""
    canonical = json.dumps({"parameters": ct.parameters, "status": ct.status}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _local_now():
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(config.UNICORN_TIMEZONE))
    except Exception:
        now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3], int(now.utcoffset().total_seconds() // 60)


def _num(tag, value):
    return f"<{tag}>{value:g}</{tag}>" if isinstance(value, (int, float)) else ""


def build_column_type_xml(ct: ColumnType, scope: str = None) -> bytes:
    """ColumnType.xml, element order as in UNICORN 7.10 exports. Only elements confirmed from
    real exports are emitted; pH, ordering info and particle size have no confirmed tag yet."""
    u = ct.unicorn
    now, offset = _local_now()
    who = escape(config.UNICORN_CREATED_BY)
    stamp = (f"<Created>{now}</Created><CreatedUtcOffsetMinutes>{offset}</CreatedUtcOffsetMinutes>"
             f"<CreatedBy>{who}</CreatedBy><LastModified>{now}</LastModified>"
             f"<LastModifiedUtcOffsetMinutes>{offset}</LastModifiedUtcOffsetMinutes>"
             f"<LastModifiedBy>{who}</LastModifiedBy>")
    technique = f"<TechniqueName>{escape(u['technique_xml'])}</TechniqueName>" if u.get("technique_xml") else ""
    pressure_unit = escape(u.get("pressure_unit") or "MPa")
    return (
        '<ColumnTypes xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        f'FormatVersion="2" UNICORNVersion="{escape(config.UNICORN_VERSION)}">'
        "<ColumnType>"
        f"<Name>{escape(ct.name)}</Name>"
        f"<AccessScopeName>{escape(scope or config.IMPORT_ACCESS_SCOPE)}</AccessScopeName>"
        f"<BedHeightUnit>cm</BedHeightUnit>{_num('BedHeight', u['bed_height'])}"
        "<ColumnVolumeUnitName>ml</ColumnVolumeUnitName>"
        f"<DeltaColumnPressureUnit>{pressure_unit}</DeltaColumnPressureUnit>{_num('DeltaColumnPressure', u['delta_pressure'])}"
        "<ColumnFlowrateUnit>ml_per_min</ColumnFlowrateUnit>"
        f"{_num('DefaultFlowrate', u['default_flow'])}{_num('MaxFlowrate', u['max_flow'])}"
        "<TypicalWidthAtBaseUnit>ml</TypicalWidthAtBaseUnit>"
        "<Hardware><Name>Any</Name>"
        f"<DiameterUnit>cm</DiameterUnit>{_num('Diameter', u['diameter'])}"
        f"<PreColumnPressureUnit>{pressure_unit}</PreColumnPressureUnit>{_num('PreColumnPressure', u['pre_pressure'])}"
        f"<IsPredefined>false</IsPredefined>{stamp}</Hardware>"
        f"<Media><Name>Any</Name>{technique}"
        "<AverageParticleDiameterUnit>um</AverageParticleDiameterUnit>"
        f"<IsPredefined>false</IsPredefined>{stamp}</Media>"
        f"{stamp}</ColumnType></ColumnTypes>"
    ).encode("utf-8")


def edit_set_string(ct: ColumnType) -> str:
    """'label=value;...' for sync_column.ps1 -Set. Placeholders are excluded so an Edit never
    overwrites a value an operator entered in UNICORN with 0."""
    skip = set(ct.unicorn.get("placeholders", []))
    return ";".join(f"{label}={ct.unicorn[key]:g}" for label, key in _EDITABLE
                    if label not in skip and isinstance(ct.unicorn.get(key), (int, float)))


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")[:80]


def queue_import_job(ct: ColumnType, digest: str, canvas_id: str = None) -> str:
    """Write zip + job to S3 and notify SQS. Returns the SQS message id (or S3 / local job path).

    canvas_id (canvas-triggered runs only) rides along in the job so the on-prem consumer can
    report the real UNICORN outcome back to the same canvas once the import/edit actually
    finishes - see canvas_handler.report_column_result.
    """
    archive = build_archive_bytes(build_column_type_xml(ct))
    problems = verify_archive_bytes(archive)
    if problems:
        raise FileWriteError(f"Generated archive failed self-check: {problems}")

    ident = ct.source.get("benchling_registry_id") or ct.source["benchling_entity_id"]
    stem = f"{_safe(ident)}_{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}_{digest[:8]}"
    job = {
        "columnName": ct.name,
        "set": edit_set_string(ct),
        # Unit of the pressure values in "set" - sync_column.ps1 refuses an Edit if the existing
        # Column Type uses a different unit (it would write bar numbers into an MPa field).
        "pressureUnit": ct.unicorn.get("pressure_unit"),
        "zip": f"{stem}.zip",
        "source": ident,
        "scope": config.IMPORT_ACCESS_SCOPE,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "contentHash": digest,
        "canvasId": canvas_id,
        # Which tenant's function created this job - both Zifo and Lilly currently share one
        # SQS queue, so the on-prem consumer can't otherwise tell which Lambda to report back
        # to. AWS sets this automatically at runtime; no config needed on either side.
        "lambdaFunctionName": os.getenv("AWS_LAMBDA_FUNCTION_NAME"),
        "benchlingEntityId": ct.source["benchling_entity_id"],
        "benchlingRegistryId": ct.source.get("benchling_registry_id"),
        "benchling": ct.source,
        "placeholders": ct.unicorn.get("placeholders", []),
        "parameters": ct.parameters,
        "warnings": ct.warnings,
    }

    job_body = json.dumps(job, indent=2, default=str).encode("utf-8")
    try:
        if not config.IMPORT_BUCKET:  # local runs: the watch-folder layout watch_service.ps1 reads
            folder = os.path.join(config.LOCAL_OUTPUT_DIR, "incoming")
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, f"{stem}.zip"), "wb") as fh:  # zip first: the watcher triggers on the job
                fh.write(archive)
            job_path = os.path.join(folder, f"{stem}.job.json")
            with open(job_path, "wb") as fh:
                fh.write(job_body)
            return job_path

        prefix = config.IMPORT_PREFIX.strip("/")
        zip_key, job_key = f"{prefix}/{stem}.zip", f"{prefix}/{stem}.job.json"
        s3 = boto3.client("s3")
        s3.put_object(Bucket=config.IMPORT_BUCKET, Key=zip_key, Body=archive, ContentType="application/zip")
        s3.put_object(Bucket=config.IMPORT_BUCKET, Key=job_key, Body=job_body, ContentType="application/json")
        if not config.SQS_QUEUE_URL:
            return f"s3://{config.IMPORT_BUCKET}/{job_key}"

        message = {**job, "s3": {"bucket": config.IMPORT_BUCKET, "zipKey": zip_key, "jobKey": job_key}}
        for bulky in ("parameters", "benchling"):  # full detail stays in job.json on S3
            message.pop(bulky, None)
        kwargs = {"QueueUrl": config.SQS_QUEUE_URL, "MessageBody": json.dumps(message, default=str)}
        if config.SQS_QUEUE_URL.endswith(".fifo"):
            # one group = strictly one job at a time (single UNICORN desktop);
            # identical content within 5 minutes (a double click) is dropped by SQS
            kwargs.update(MessageGroupId="unicorn-column-sync", MessageDeduplicationId=digest)
        return boto3.client("sqs").send_message(**kwargs)["MessageId"]
    except Exception as e:  # boto/OS errors both map to the same documented exception
        raise FileWriteError(f"Could not queue import job for {ct.name}: {e}") from e
