"""
map-column-type: Benchling -> UNICORN Column Type field mapping, derivations and unit
conversions, as specified in Unicorn_mapping_fields.xlsx.

Pure functions on ColumnSource - no API calls - so this is fully unit-testable.
"""
import math
import re
from typing import Optional, Tuple

import config
from sync.models import ColumnSource, ColumnType, MappingError
from sync.validate import to_number

RUN, DETAILS, ORDERING = "Run Parameters", "Details", "Ordering Information"

_PH_RANGE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:-|–|—|to|\.\.)\s*(\d+(?:\.\d+)?)\s*$", re.IGNORECASE)


def _p(value, unit=None):
    return {"value": value, "unit": unit}


def _round(x: Optional[float], ndigits: int = 2) -> Optional[float]:
    return None if x is None else round(x, ndigits)


def column_area_cm2(diameter_cm: float) -> float:
    return math.pi * (diameter_cm / 2.0) ** 2


def parse_ph_range(raw) -> Tuple[float, float]:
    match = _PH_RANGE.match(str(raw))
    if not match:
        raise MappingError(f"stable_ph_range {raw!r} is not a 'low-high' range")
    low, high = float(match.group(1)), float(match.group(2))
    if not (0 <= low <= high <= 14):
        raise MappingError(f"stable_ph_range {raw!r} is outside 0-14 or inverted")
    return low, high


def map_technique(raw) -> Tuple[str, str]:
    """-> (display code, UNICORN XML TechniqueName)."""
    text = str(raw).strip().lower()
    for code, xml in config.TECHNIQUE_MAP.values():
        if text in (code.lower(), xml.lower()):
            return code, xml
    # longest key first so 'reversed phase' beats short keys like 'hic'
    for key in sorted(config.TECHNIQUE_MAP, key=len, reverse=True):
        if key in text:
            return config.TECHNIQUE_MAP[key]
    raise MappingError(f"Technique {raw!r} has no UNICORN TechniqueName (extend TECHNIQUE_MAP)")


def _name_num(x: float) -> str:
    """Geometry as entered, up to 3 dp, always one decimal: 1 -> '1.0', 17.708 -> '17.708'."""
    return str(round(float(x), 3))


def build_column_type_name(name: str, diameter: float, bed_height: float, volume: float,
                           registry_id: str = None, resin: str = None) -> str:
    """Deterministic (UNICORN prompts on name collision), e.g.
    'Poros 50 HS_1.0 x 20.3cm_17.708mL_COL001' with the default template."""
    try:
        return config.COLUMN_NAME_TEMPLATE.format(
            name=name, registry_id=registry_id or "", resin=resin or "",
            d=_name_num(diameter), h=_name_num(bed_height), v=_name_num(volume)).strip()
    except (KeyError, IndexError) as e:
        raise MappingError(f"COLUMN_NAME_TEMPLATE has unknown placeholder {e}") from e


def map_pressure_unit(raw) -> Tuple[str, str]:
    """Pressure Unit dropdown -> (key 'bar'|'mpa', UNICORN XML token)."""
    key = str(raw).strip().lower()
    if key not in config.PRESSURE_UNIT_XML:
        raise MappingError(f"Pressure Unit {raw!r} is not one of: bar, MPa")
    return key, config.PRESSURE_UNIT_XML[key]


def map_column(src: ColumnSource) -> ColumnType:
    """Assumes validate_column() has passed."""
    f = config.FIELDS
    warnings = []
    lib = src.resin_library.fields
    material = src.resin_material.fields if src.resin_material else None

    diameter = to_number(src.fields.value(f["diameter"]))
    bed_height = to_number(src.fields.value(f["bed_height"]))
    volume = to_number(src.fields.value(f["volume"]))
    area = column_area_cm2(diameter)

    # --- Technique -----------------------------------------------------------
    technique_raw = lib.display(f["lib_technique"])
    technique, technique_xml = map_technique(technique_raw) if technique_raw else (None, None)
    if technique is None:
        warnings.append("Technique empty on Resin Characteristics Library row")
    elif technique_xml not in config.CONFIRMED_TECHNIQUE_XML:
        warnings.append(f"TechniqueName '{technique_xml}' not yet confirmed against a UNICORN export")

    # --- Pressure: in the unit picked on the column (bar or MPa) -----------------
    # Max pre-column pressure is entered on the column in that unit and used as-is. Max delta
    # column pressure comes from the resin library (always bar): kept for bar, / 10 for MPa.
    unit_raw = src.fields.display(f["pressure_unit"])
    if unit_raw is None:
        unit_raw = config.DEFAULT_PRESSURE_UNIT
        warnings.append(f"Pressure Unit empty on column - using {config.PRESSURE_UNIT_XML[unit_raw]}")
    unit_key, pressure_unit = map_pressure_unit(unit_raw)
    if pressure_unit not in config.CONFIRMED_PRESSURE_UNIT_XML:
        warnings.append(f"Pressure unit '{pressure_unit}' not yet confirmed against a UNICORN export")
    from_bar = 1.0 if unit_key == "bar" else 0.1

    lib_pressure = None
    pressure_raw = lib.value(f["lib_max_pressure_bar"])
    if pressure_raw is not None:
        bar = to_number(pressure_raw)
        if bar is None:
            raise MappingError(f"max_pressure_bar {pressure_raw!r} is not numeric")
        lib_pressure = _round(bar * from_bar, 4)
    else:
        warnings.append("max_pressure_bar empty on Resin Characteristics Library row")
    delta_pressure = lib_pressure

    pre_pressure = None
    pre_raw = src.fields.value(f["max_pre_pressure"])
    if pre_raw is not None:
        pre_pressure = to_number(pre_raw)
        if pre_pressure is None:
            raise MappingError(f"Max Pre-Column Pressure {pre_raw!r} is not numeric")
    elif lib_pressure is not None:
        pre_pressure = lib_pressure
        warnings.append("Max Pre-Column Pressure empty on column - taken from resin library max_pressure_bar")

    # --- Flow: prefer column instance value, fall back to resin suggestion -----
    linear_raw = src.fields.value(f["linear_flow"])
    if linear_raw is None:
        linear_raw = lib.value(f["lib_suggested_flow"])
        if linear_raw is not None:
            warnings.append("Default linear flow taken from resin library suggested_flow_rate_cmhr")
    linear_flow = to_number(linear_raw) if linear_raw is not None else None
    if linear_raw is not None and linear_flow is None:
        raise MappingError(f"Linear flow rate {linear_raw!r} is not numeric")
    if linear_flow is None:
        warnings.append("No default linear flow rate on column or resin library")

    default_flow = _round(linear_flow * area / 60.0) if linear_flow is not None else None
    max_linear = None
    max_linear_raw = src.fields.value(f["max_linear_flow"])
    if max_linear_raw is not None:
        max_linear = to_number(max_linear_raw)
        if max_linear is None:
            raise MappingError(f"Max Linear Flow Rate {max_linear_raw!r} is not numeric")
    elif config.MAX_LINEAR_FLOW_FACTOR is not None and linear_flow is not None:
        max_linear = _round(linear_flow * config.MAX_LINEAR_FLOW_FACTOR)
        warnings.append(f"Max linear flow is interim rule {config.MAX_LINEAR_FLOW_FACTOR} x default linear flow")
    max_flow = _round(max_linear * area / 60.0) if max_linear is not None else None

    # --- pH: single Benchling range reused for short and long term -------------
    ph_low = ph_high = None
    ph_raw = lib.value(f["lib_ph_range"])
    if ph_raw is not None:
        ph_low, ph_high = parse_ph_range(ph_raw)
    else:
        warnings.append("stable_ph_range empty on Resin Characteristics Library row")

    particle_raw = lib.value(f["lib_particle_size"])
    particle = to_number(particle_raw) if particle_raw is not None else None

    # --- Ordering information -------------------------------------------------
    resin_name = None
    if material is not None:
        resin_name = material.display(f["material_name"]) or src.resin_material.name
    resin_code = src.fields.display(f["resin_catalog_number"])
    if not resin_code and material is not None:
        resin_code = material.display(f["material_item_code"])

    hardware_name = src.hardware.name if src.hardware else src.fields.display(f["hardware_link"])
    hardware_code = src.hardware.fields.display(f["hardware_catalog"]) if src.hardware else None

    resin_label = resin_name or src.fields.display(f["resin_type_text"])
    name = build_column_type_name(src.name, diameter, bed_height, volume, src.registry_id, resin_label)
    status = str(src.fields.display(f["status"]))

    # Values that go into ColumnType.xml. UNICORN derives column volume and linear flows
    # itself; flows and pressures are NOT NULL there, so gaps become the placeholder.
    placeholders = []

    def required(label, value):
        if value is not None:
            return value
        placeholders.append(label)
        return config.REQUIRED_PLACEHOLDER

    unicorn = {
        "technique_xml": technique_xml,
        "diameter": diameter,
        "bed_height": bed_height,
        "default_flow": required("Default flow rate", default_flow),
        "max_flow": required("Max flow rate", max_flow),
        "pressure_unit": pressure_unit,
        "pre_pressure": required("Max pre-column pressure", pre_pressure),
        "delta_pressure": required("Max delta column pressure", delta_pressure),
        "placeholders": placeholders,
    }
    if placeholders:
        warnings.append(f"Written as {config.REQUIRED_PLACEHOLDER:g} placeholder (no source) - edit in UNICORN "
                        f"before use: {', '.join(placeholders)}")

    parameters = {
        RUN: {
            "Technique": _p(technique),
            "Column volume": _p(_round(volume, 3), "ml"),
            "Max pre-column pressure": _p(pre_pressure, pressure_unit),
            "Max delta column pressure": _p(delta_pressure, pressure_unit),
            "Default flow rate": _p(default_flow, "ml/min"),
            "Max flow rate": _p(max_flow, "ml/min"),
            "Default linear flow rate": _p(_round(linear_flow), "cm/h"),
            "Max linear flow rate": _p(max_linear, "cm/h"),
            "Min pH value (short term)": _p(ph_low),
            "Max pH value (short term)": _p(ph_high),
            "Min pH value (long term)": _p(ph_low),
            "Max pH value (long term)": _p(ph_high),
        },
        DETAILS: {
            "Hardware diameter": _p(diameter, "cm"),
            "Bed height": _p(bed_height, "cm"),
            "Typical loading range": _p(None),
            "Total liquid volume (Vt)": _p(None, "ml"),
            "Void volume (Vo)": _p(None, "ml"),
            "Typical peak width at base": _p(None, "ml"),
            "Average particle diameter": _p(particle, "um"),
            "Molecular weight range": _p(None),
        },
        ORDERING: {
            "Name": _p(name),
            "Code number": _p(src.registry_id if config.USE_REGISTRY_ID_AS_CODE_NUMBER else None),
            "Resin name": _p(resin_label),
            "Resin code number": _p(resin_code),
            "Hardware name": _p(hardware_name),
            "Hardware code number": _p(hardware_code),
        },
    }

    source = {
        "benchling_entity_id": src.id,
        "benchling_registry_id": src.registry_id,
        "benchling_name": src.name,
        "benchling_url": src.web_url,
        "resin_library_result_id": src.resin_library.id,
        "resin_material_id": src.resin_material.id if src.resin_material else None,
        "hardware_id": src.hardware.id if src.hardware else None,
        "column_status": status,
    }
    return ColumnType(name=name, status=status, parameters=parameters, source=source, unicorn=unicorn,
                      warnings=warnings + list(src.resolution_notes))
