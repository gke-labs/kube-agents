#!/usr/bin/env python3
"""Publish a rendered dashboard directory to its serving location.

Usage::

    python3 scripts/eval_dashboard/publish.py --out-dir out/ --target gs://bucket/dash
    python3 scripts/eval_dashboard/publish.py --out-dir out/ --target /some/local/dir

A ``gs://`` target shells out to gsutil; anything else is a local directory
copy (which is also what the tests exercise -- nothing in the test suite
touches a bucket).

Every object is uploaded with ``Cache-Control: no-cache``. The page refetches
``data.json`` every 60 seconds, and GCS's default public-object caching
(3600s) would quietly turn that into an hour-stale dashboard; ``no-cache``
makes each poll revalidate instead.
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys

CACHE_CONTROL = "Cache-Control: no-cache"


def out_dir_files(out_dir: pathlib.Path) -> list[pathlib.Path]:
    if not out_dir.is_dir():
        raise SystemExit(f"ERROR: {out_dir} is not a directory (render first)")
    files = sorted(p for p in out_dir.iterdir() if p.is_file())
    if not files:
        raise SystemExit(f"ERROR: {out_dir} is empty (render first)")
    return files


def gsutil_command(files: list[pathlib.Path], target: str) -> list[str]:
    """The exact argv a bucket publish runs -- kept separate so tests can
    assert on it without ever executing gsutil."""
    return [
        "gsutil",
        "-h",
        CACHE_CONTROL,
        "cp",
        *[str(f) for f in files],
        target.rstrip("/") + "/",
    ]


def publish(out_dir: str, target: str, runner=subprocess.run) -> None:
    files = out_dir_files(pathlib.Path(out_dir))
    if target.startswith("gs://"):
        runner(gsutil_command(files, target), check=True)
        return
    dest = pathlib.Path(target)
    dest.mkdir(parents=True, exist_ok=True)
    for path in files:
        shutil.copyfile(path, dest / path.name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out-dir", required=True, help="rendered directory")
    parser.add_argument("--target", required=True, help="gs://bucket/path or local dir")
    args = parser.parse_args(argv)
    publish(args.out_dir, args.target)
    print(f"published {args.out_dir} -> {args.target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
