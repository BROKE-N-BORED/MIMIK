#!/usr/bin/env python3
"""Build the MIMIK v0.3 release zip.

Zip modes matter: GUI extractors (Thunar's) drop the execute bit unless
external_attr carries the full regular-file type bits — 0o100755 for
scripts/launchers, 0o100644 for plain files. Bare 0o755 is NOT enough.
This script sets them explicitly and verifies the result.
"""
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION = "0.3"
PREFIX = f"mimik-v{VERSION}/"

EXECUTABLES = {"install.sh", "uninstall.sh", "Install MIMIK.desktop"}

INCLUDE_DIRS = ["mimik", "tests", "icons"]
INCLUDE_FILES = ["pyproject.toml", "README.md", "CHANGELOG.md",
                 "LICENSE-NOTICE",
                 "install.sh", "uninstall.sh", "Install MIMIK.desktop"]


def collect():
    paths = []
    for name in INCLUDE_FILES:
        paths.append(os.path.join(ROOT, name))
    for d in INCLUDE_DIRS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, d)):
            dirnames[:] = [x for x in dirnames if x != "__pycache__"]
            for fn in sorted(filenames):
                if fn.endswith((".pyc", ".pyo")):
                    continue
                paths.append(os.path.join(dirpath, fn))
    return sorted(paths)


def build(out_path):
    paths = collect()
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for full in paths:
            rel = os.path.relpath(full, ROOT)
            arc = PREFIX + rel
            mode = 0o100755 if os.path.basename(rel) in EXECUTABLES else 0o100644
            info = zipfile.ZipInfo(arc)
            info.external_attr = mode << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(full, "rb") as f:
                zf.writestr(info, f.read())
    return paths


def verify(out_path):
    """Check stored modes + that the zip test-extracts cleanly."""
    import subprocess
    import tempfile

    with zipfile.ZipFile(out_path) as zf:
        bad = zf.testzip()
        assert bad is None, f"corrupt member: {bad}"
        for info in zf.infolist():
            mode = info.external_attr >> 16  # full st_mode incl. file type
            want = 0o100755 if os.path.basename(info.filename) in EXECUTABLES else 0o100644
            assert mode == want, f"{info.filename}: mode {oct(mode)}, want {oct(want)}"
    tmp = tempfile.mkdtemp()
    subprocess.run(["unzip", "-q", out_path, "-d", tmp], check=True)
    for name in ("install.sh", "uninstall.sh", "Install MIMIK.desktop"):
        p = os.path.join(tmp, PREFIX, name)
        assert os.access(p, os.X_OK), f"{name} lost its exec bit on extract"
    return tmp


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.expanduser("~"), "workspace", "your_files",
        f"mimik-v{VERSION}.zip")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    paths = build(out)
    tmp = verify(out)
    size = os.path.getsize(out)
    print(f"built {out} ({size} bytes, {len(paths)} files)")
    print(f"modes verified, test-extracted to {tmp}")
