"""
Build the two deployment artifacts into dist/:

    dist/packed-column-sync-layer.zip   dependencies (requirements.txt) -> Lambda layer
    dist/packed-column-sync.zip         application code only           -> Lambda function

    python build.py            # both
    python build.py --code     # code only (the usual case after a change)

Dependencies are built for the Lambda runtime (Linux x86_64, Python 3.14), not this machine.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

PYTHON_VERSION = "3.14"
PLATFORM = "manylinux2014_x86_64"
CODE = ["lambda_function.py", "config.py", "auth_utils", "canvas_handler", "sync"]
ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")


def _zip_tree(z, folder, arc_prefix=""):
    for d, dirs, files in os.walk(folder):
        dirs[:] = [x for x in dirs if x not in ("__pycache__", "bin", "share")]
        for f in files:
            if not f.endswith(".pyc"):
                full = os.path.join(d, f)
                z.write(full, arc_prefix + os.path.relpath(full, folder).replace(os.sep, "/"))


def build_layer():
    tmp = tempfile.mkdtemp(prefix="uclayer")  # short path: avoids Windows MAX_PATH during pip install
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--target", tmp,
                        "--platform", PLATFORM, "--implementation", "cp", "--python-version", PYTHON_VERSION,
                        "--only-binary=:all:", "-r", os.path.join(ROOT, "requirements.txt")], check=True)
        out = os.path.join(DIST, "packed-column-sync-layer.zip")
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            _zip_tree(z, tmp, "python/")  # layers must put packages under python/
        print(f"layer: {out} ({os.path.getsize(out) / 1e6:.1f} MB)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def build_code():
    out = os.path.join(DIST, "packed-column-sync.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for item in CODE:
            path = os.path.join(ROOT, item)
            if os.path.isdir(path):
                _zip_tree(z, path, item + "/")
            else:
                z.write(path, item)
    print(f"code:  {out} ({os.path.getsize(out) / 1e3:.0f} KB)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", action="store_true", help="build only the code zip")
    args = ap.parse_args()
    os.makedirs(DIST, exist_ok=True)
    if not args.code:
        build_layer()
    build_code()
