"""
config.py
---------
Central configuration, loaded from a `.env` file at the project root (with
sensible defaults so the project runs without one).

Deliberately dependency-free: a tiny stdlib parser reads `.env`, so the
project keeps its "no pip install required to run" property. `.env` overrides
defaults; real environment variables override `.env`.

Usage:
    from config import settings
    settings.winappdriver_url   # 'http://127.0.0.1:4723'
    settings.exports_dir        # Path to <root>/exports
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parent.parent
_ENV_FILE = ROOT / ".env"


def _load_env_file(path: pathlib.Path) -> dict[str, str]:
    """Minimal .env parser: KEY=VALUE lines, # comments, optional quotes."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            values[key] = val
    return values


_file_values = _load_env_file(_ENV_FILE)


def _get(key: str, default: str) -> str:
    # Precedence: real environment > .env file > default.
    return os.environ.get(key) or _file_values.get(key) or default


@dataclass(frozen=True)
class Settings:
    winappdriver_url: str
    unicorn_process: str
    column_dialog_title: str
    import_access_scope: str          # 'Personal' (test) or 'Global' (production)
    exports_dir: pathlib.Path
    watch_dir: pathlib.Path           # local "queue" the agent watches
    benchling_url: str                # e.g. https://<tenant>.benchling.com
    benchling_api_key: str            # secret; keep in .env only
    benchling_client_id: str          # Benchling App (OAuth2) client id
    benchling_client_secret: str      # secret; keep in .env only
    benchling_entity_schema: str      # Packed Column entity schema id
    benchling_packing_schema: str     # Column Packing result schema id
    benchling_resin_schema: str       # Resin Characteristics Library result schema id
    benchling_hardware_schema: str    # Column Hardware (Non-SAP Raw Material) schema id


settings = Settings(
    winappdriver_url=_get("WINAPPDRIVER_URL", "http://127.0.0.1:4723"),
    unicorn_process=_get("UNICORN_PROCESS", "UNICORNClient"),
    column_dialog_title=_get("COLUMN_DIALOG_TITLE", "Column Handling"),
    import_access_scope=_get("IMPORT_ACCESS_SCOPE", "Personal"),
    exports_dir=pathlib.Path(_get("EXPORTS_DIR", str(ROOT / "exports"))),
    watch_dir=pathlib.Path(_get("WATCH_DIR", str(ROOT / "watch"))),
    benchling_url=_get("BENCHLING_URL", ""),
    benchling_api_key=_get("BENCHLING_API_KEY", ""),
    benchling_client_id=_get("BENCHLING_CLIENT_ID", ""),
    benchling_client_secret=_get("BENCHLING_CLIENT_SECRET", ""),
    # Real schema ids for the lilly-prd-dev-vc tenant (from the mapping doc).
    benchling_entity_schema=_get("BENCHLING_ENTITY_SCHEMA", "ts_AJliSA2awf"),
    benchling_packing_schema=_get("BENCHLING_PACKING_SCHEMA", "assaysch_InEaOqToeK"),
    benchling_resin_schema=_get("BENCHLING_RESIN_SCHEMA", "assaysch_yNYYArXbpH"),
    benchling_hardware_schema=_get("BENCHLING_HARDWARE_SCHEMA", "ts_a78KBDyDmA"),
)
