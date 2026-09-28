"""Local tooling. Loads ../.env (KEY=VALUE lines) before config.py reads the environment.
Real environment variables win over .env. Never used by the Lambda itself."""
import os

_ENV_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")

if os.path.exists(_ENV_FILE):
    with open(_ENV_FILE, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip().removeprefix("export ").strip(), value.strip().strip('"').strip("'")
            if value and key not in os.environ:
                os.environ[key] = value
