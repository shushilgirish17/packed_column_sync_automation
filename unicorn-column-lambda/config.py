"""
Central configuration. Everything tenant-specific (schema IDs, field names, AWS resources)
lives here so the same code runs against the Zifo tenant and the Lilly dev tenant.
"""
import json
import os


def _env(name, default=None):
    value = os.getenv(name, default)
    return value if value not in ("", None) else default


def _env_bool(name, default=False):
    return str(_env(name, str(default))).lower() in ("1", "true", "yes", "y")


def _env_json(name, default):
    raw = _env(name)
    return json.loads(raw) if raw else default


# ---------------------------------------------------------------------------
# Benchling
# ---------------------------------------------------------------------------
# Credentials, first match wins:
#   BENCHLING_SECRET_ID      Secrets Manager secret (CloudFormation deployments). Holds at least
#                            {"client_secret"}; url/client_id may come from BENCHLING_URL /
#                            BENCHLING_CLIENT_ID env vars instead, so only the secret is secret.
#   BENCHLING_SSM_PARAMETER  SSM SecureString holding {"url", "client_id", "client_secret"}
#                            (the hand-built Zifo function).
#   neither                  BENCHLING_URL / BENCHLING_CLIENT_ID / BENCHLING_CLIENT_SECRET (local runs).
BENCHLING_SECRET_ID = _env("BENCHLING_SECRET_ID")
BENCHLING_SSM_PARAMETER = _env("BENCHLING_SSM_PARAMETER")
APP_DEFINITION_ID = _env("APP_DEFINITION_ID")              
SKIP_WEBHOOK_VERIFICATION = _env_bool("SKIP_WEBHOOK_VERIFICATION", False)

PACKED_COLUMN_SCHEMA_ID = _env("PACKED_COLUMN_SCHEMA_ID")            # entity schema (ts_...)
RESIN_LIBRARY_SCHEMA_ID = _env("RESIN_LIBRARY_SCHEMA_ID")            # result schema (assaysch_...)
# Optional. Not used in the Zifo tenant (packing values live on the entity). If set, the
# latest Column Packing row for each column is merged in as an extra source of fields.
COLUMN_PACKING_SCHEMA_ID = _env("COLUMN_PACKING_SCHEMA_ID")

# ---------------------------------------------------------------------------
# Field names
#
# Benchling keys entity fields by DISPLAY NAME and result fields by API NAME, so each
# logical field lists candidates; the first one present on the record wins. Candidate
# lists carry every tenant's naming (Zifo + Lilly dev) so no env override is needed.
# ---------------------------------------------------------------------------
FIELDS = {
    # Packed Column entity (+ optional Column Packing result)
    "diameter": ["Column Diameter (cm)", "column_diameter_cm"],
    "bed_height": ["Packed Bed Height (cm)", "packed_bed_height_cm"],
    # Calculated Volume = Column Packing result value, surfaced on the Packed Column as a computed field.
    "volume": ["Calculated Volume (mL)", "calculated_volume_ml", "Packed Volume (mL)"],
    "linear_flow": ["Default Linear Flow Rate (cm/h)", "default_linear_flow_rate_cmh",
                    "Column Operation Flow Rate (cm/h)", "column_operation_flow_rate_cmh"],
    "max_linear_flow": ["Max Linear Flow Rate (cm/h)", "max_linear_flow_rate_cmh"],
    # Max pre-column pressure is entered in the unit picked in "Pressure Unit" (bar or MPa).
    "max_pre_pressure": ["Max Pre-Column Pressure", "max_pre_column_pressure"],
    "pressure_unit": ["Pressure Unit", "pressure_unit"],
    "status": ["Column Status", "column_status"],
    "resin_link": ["Resin Lot", "Packed Column Resin Type", "Resin Type", "Resin Catalog", "resin_catalog"],
    "resin_type_text": ["Resin Type", "Packed Column Resin Type"],
    "pack_qc": ["Column Pack QC", "column_pack_qc"],
    "resin_catalog_number": ["Resin Catalog Number", "used_resin_item_code"],
    "hardware_link": ["Column Hardware", "column_hardware"],
    # Column Hardware (Non-SAP Raw Material) entity
    "hardware_catalog": ["Catalog #", "Catalog Number", "catalog_number"],
    # Resin Characteristics Library result
    "lib_resin_link": ["resin_catalog", "Resin Catalog"],
    # packed_column_technique = the "UNICORN Technique" dropdown (Zifo); unicorn_technique = the
    # older text field with the same display name.
    "lib_technique": ["packed_column_technique", "unicorn_technique", "technique", "Technique"],
    "lib_max_pressure_bar": ["max_pressure_bar", "Max Pressure (bar)"],
    "lib_suggested_flow": ["suggested_flow_rate_cmhr", "suggested_flow_ratecmhr", "Suggested Flow Rate (cm/hr)"],
    "lib_ph_range": ["stable_ph_range", "Stable pH Range"],
    "lib_particle_size": ["mean_particle_size_um", "mean_particle_sizeum", "Mean Particle Size (um)"],
    # SAP Raw Material entity (target of the resin link)
    "material_name": ["Raw Material Name", "raw_material_name", "Name"],
    "material_item_code": ["Item Code", "item_code", "Catalog #"],
}

ACTIVE_STATUS = _env("ACTIVE_STATUS", "Active")

# Technique dropdown option -> (display code, UNICORN XML <TechniqueName>). Keys are matched
# case-insensitively as substrings of the dropdown display value, longest first.
# XML names CONFIRMED from real exports: Affinity, CationExchange. The rest follow the same
# pattern but are UNCONFIRMED - verify by exporting one column of each technique from UNICORN.
# A bare "ion exchange" is ambiguous (UNICORN distinguishes cation/anion) and is rejected.
# Extend/override via TECHNIQUE_MAP='{"mixed mode": ["MMC", "MixedMode"]}'.
TECHNIQUE_MAP = {
    "affinity": ("AC", "Affinity"),
    "protein a": ("AC", "Affinity"),
    "cation": ("CIEX", "CationExchange"),
    "ciex": ("CIEX", "CationExchange"),
    "anion": ("AIEX", "AnionExchange"),
    "aiex": ("AIEX", "AnionExchange"),
    "hydrophobic": ("HIC", "HydrophobicInteraction"),
    "hic": ("HIC", "HydrophobicInteraction"),
    "size exclusion": ("SEC", "SizeExclusion"),
    "gel filtration": ("SEC", "SizeExclusion"),
    "reversed phase": ("RPC", "ReversedPhase"),
    "reverse phase": ("RPC", "ReversedPhase"),
    "chromatofocusing": ("CF", "Chromatofocusing"),
    "desalting": ("DS", "Desalting"),
}
TECHNIQUE_MAP.update({k.lower(): tuple(v) for k, v in _env_json("TECHNIQUE_MAP", {}).items()})
CONFIRMED_TECHNIQUE_XML = {"Affinity", "CationExchange"}
# XML TechniqueName -> the text UNICORN shows in its Technique dropdown (confirmed from the New
# Column Type dialog, 2026-10-04). Used to change the technique of an EXISTING Column Type, since
# the Edit dialog's Technique cell takes the display text, not the XML token.
TECHNIQUE_DISPLAY = {
    "Any": "Any",
    "Affinity": "Affinity Chromatography (AC)",
    "AnionExchange": "Anion Exchange Chromatography (AIEX)",
    "CationExchange": "Cation Exchange Chromatography (CIEX)",
    "Chromatofocusing": "Chromatofocusing (CF)",
    "Desalting": "Desalting (DS)",
    "SizeExclusion": "Size Exclusion Chromatography (SEC)",
    "HydrophobicInteraction": "Hydrophobic Interaction Chromatography (HIC)",
    "ReversedPhase": "Reversed Phase Chromatography (RPC)",
}
# Written when the Resin Characteristics Library row has no technique. "Any" is a valid choice in
# UNICORN's required Technique dropdown (New Column Type dialog, confirmed 2026-10-04); the XML
# token "Any" is assumed (same as Hardware/Media <Name>Any</Name>). DEFAULT_TECHNIQUE_XML="" leaves
# the element out instead (UNICORN then defaults to Affinity).
DEFAULT_TECHNIQUE_XML = os.getenv("DEFAULT_TECHNIQUE_XML", "Any").strip() or None

# Max linear flow rate has no Benchling source. The design doc says write it empty; the
# mapping sheet proposes an interim 0.75 x default linear flow. Leave unset until
# confirmed - it is a pressure safety limit.
MAX_LINEAR_FLOW_FACTOR = float(_env("MAX_LINEAR_FLOW_FACTOR")) if _env("MAX_LINEAR_FLOW_FACTOR") else None

# UNICORN's DB makes DefaultFlowrate / MaxFlowrate / pre- and delta pressure NOT NULL, so a
# missing value is written as this placeholder (agreed decision: 0 = "unset, edit before use").
# Placeholders are never sent as Edit values for an existing column type.
REQUIRED_PLACEHOLDER = float(_env("REQUIRED_PLACEHOLDER", "0"))

# Pressure Unit dropdown (matched case-insensitively) -> UNICORN ColumnType.xml unit token.
# Both CONFIRMED from real exports (bar: a MabSelect PrismA export, 2026-10-04).
# Benchling library pressures are always in bar.
PRESSURE_UNIT_XML = {"bar": "bar", "mpa": "MPa"}
CONFIRMED_PRESSURE_UNIT_XML = {"MPa", "bar"}
DEFAULT_PRESSURE_UNIT = "mpa"  # used (with a warning) when Pressure Unit is empty

# UNICORN Column Type name - this is the <Name> shown in the Column Handling library, so it's
# also the dedup key (same name -> Edit path, new name -> Import path). Fields: {name} Benchling
# name, {registry_id}, {resin} the column's Resin Type field (falls back to the resin material
# name), {d} diameter, {h} bed height, {v} volume (as entered, up to
# 3 dp, always one decimal: 1 -> 1.0, 17.708 -> 17.708).
# Default e.g. 'Poros 50 HS_1.0 x 20.3cm_17.708mL_COL001'.
COLUMN_NAME_TEMPLATE = _env("COLUMN_NAME_TEMPLATE", "{resin}_{d} x {h}cm_{v}mL_{registry_id}")

# Personal = visible only to the importing account (testing); Global = everyone (production).
IMPORT_ACCESS_SCOPE = _env("IMPORT_ACCESS_SCOPE", "Personal")
UNICORN_TIMEZONE = _env("UNICORN_TIMEZONE", "America/Indiana/Indianapolis")
UNICORN_VERSION = _env("UNICORN_VERSION", "7.10.0.2964")
UNICORN_CREATED_BY = _env("UNICORN_CREATED_BY", "BenchlingSync")

# Optional qualification gate (disabled, matching the current agreed rule: status only).
REQUIRE_PACK_QC_PASS = _env_bool("REQUIRE_PACK_QC_PASS", False)

# Populate UNICORN "Code number" with the Benchling registry ID (e.g. COL010) for traceability.
USE_REGISTRY_ID_AS_CODE_NUMBER = _env_bool("USE_REGISTRY_ID_AS_CODE_NUMBER", True)

# ---------------------------------------------------------------------------
# AWS
# ---------------------------------------------------------------------------
# S3 holds <stem>.zip + <stem>.job.json under IMPORT_PREFIX; SQS gets one message per job pointing
# at them. A .fifo queue also gives one-at-a-time delivery and 5-minute duplicate suppression.
# IMPORT_BUCKET unset -> files in LOCAL_OUTPUT_DIR/incoming (local runs).
IMPORT_BUCKET = _env("IMPORT_BUCKET")
IMPORT_PREFIX = _env("IMPORT_PREFIX", "unicorn-sync/incoming")
SQS_QUEUE_URL = _env("SQS_QUEUE_URL")
ASYNC_WEBHOOK = _env_bool("ASYNC_WEBHOOK", True)       # ack webhook fast, do the sync in an async self-invoke

LOCAL_OUTPUT_DIR = _env("LOCAL_OUTPUT_DIR", "/tmp/unicorn-out" if os.getenv("AWS_LAMBDA_FUNCTION_NAME") else "out")

# Canvas block IDs
CANVAS_COLUMNS_INPUT_ID = "packed_columns"
CANVAS_SYNC_BUTTON_ID = "sync_to_unicorn"
CANVAS_RESULTS_ID = "sync_results"
