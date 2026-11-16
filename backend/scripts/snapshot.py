"""Unpack the organizer KTRU zip (zip of zips, non-UTF8 outer names) and build the snapshot once.

Usage: python scripts/snapshot.py <ktru_zip> <out_dir> [--portal-dir DIR] [--work DIR]
Skips everything if <out_dir>/current/manifest.json already exists. Only *_actual_* XML files are unpacked.
"""

import argparse
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path


def unpack(zip_path: Path, raw: Path) -> int:
    raw.mkdir(parents=True, exist_ok=True)
    n = 0
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if not name.endswith(".zip"):
                continue
            with zipfile.ZipFile(io.BytesIO(z.read(name))) as inner:
                for m in inner.namelist():
                    out = f"{os.path.basename(name)[:-4]}__{os.path.basename(m)}"
                    if "_actual_" in out and "not-actual" not in out and out.endswith(".xml"):
                        (raw / out).write_bytes(inner.read(m))
                        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ktru_zip", type=Path)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--portal-dir", type=Path, default=None)
    ap.add_argument("--work", type=Path, default=None)
    a = ap.parse_args()
    if (a.out_dir / "current" / "manifest.json").exists():
        print(f"snapshot exists: {(a.out_dir / 'current').resolve()} - skip", flush=True)
        return 0
    work = a.work or Path(tempfile.mkdtemp(prefix="ktru-raw-"))
    t0 = time.monotonic()
    n = unpack(a.ktru_zip, work)
    print(f"unpacked {n} actual XML files in {time.monotonic() - t0:.1f}s", flush=True)
    cmd = [sys.executable, "-m", "src.ktru.catalog.build", str(work), str(a.out_dir)]
    if a.portal_dir is not None:
        cmd += ["--portal-dir", str(a.portal_dir)]
    t1 = time.monotonic()
    rc = subprocess.call(cmd)
    print(f"build exit={rc} in {time.monotonic() - t1:.1f}s", flush=True)
    if a.work is None:
        shutil.rmtree(work, ignore_errors=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
