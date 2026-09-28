"""
Shared pytest setup: put lib/ and tools/ on the import path so the pure
modules under test (unicorn_coltype, config, generate_import)
import the same way they do at runtime.

Only offline, deterministic logic is unit-tested here. Anything that needs a
live UNICORN, WinAppDriver, or Benchling connection is exercised by the
scripts in tools/, not by these tests.
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (ROOT / "lib", ROOT / "tools"):
    sys.path.insert(0, str(_p))

FIXTURES = ROOT / "fixtures"
