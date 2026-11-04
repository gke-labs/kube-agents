#!/usr/bin/env python3
"""Keep each Python test shard's latest run attempt for the coverage join.

Each shard job in ``.github/workflows/python-tests.yml`` uploads its coverage
data and failing-file list as ``python-coverage-shard-<N>-attempt-<A>``, with
``<A>`` the run attempt. The join downloads every such artifact of the run, so
after "Re-run failed jobs" it holds a re-run shard's data from two attempts.
This script keeps the highest attempt per shard, renames it to
``python-coverage-shard-<N>`` for ``make coverage-merge``, and removes the rest.

The attempt is in the name because the download cannot tell same-named
artifacts apart. ``actions/download-artifact`` keeps the one with the highest
artifact ID, and IDs do not follow upload order: on run 41181129428 the failed
attempt-1 upload of shard 2 had a higher ID than the green attempt-2 upload, so
the join of attempt 2 read the stale failing-file list and went red.

A ``python-coverage-shard-*`` directory with no attempt in its name is refused,
so a workflow that stops adding the attempt fails the join instead of reading
whatever the download picked.

Usage::

    python3 scripts/coverage_shard_attempts.py .coverage-data
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

# The artifact name prefix the shard jobs upload under, and the shape of one
# shard attempt's directory as download-artifact extracts it.
SHARD_PREFIX = "python-coverage-shard-"
ATTEMPT_DIR_RE = re.compile(rf"^{re.escape(SHARD_PREFIX)}(?P<shard>\d+)-attempt-(?P<attempt>\d+)$")
# The two files a shard writes; named here for the tests' fixtures.
DATA_FILE = ".coverage"
FAILED_FILE = "failed-files.txt"
USAGE = "usage: coverage_shard_attempts.py <coverage-data-dir>"
EXIT_USAGE = 2


def keep_latest_attempts(root: Path) -> list[str]:
    """Rename each shard's highest attempt to its bare name and remove the other
    attempts. Returns the names of the removed directories."""
    if not root.is_dir():
        raise FileNotFoundError(f"{root} is not a directory")
    latest: dict[int, tuple[int, Path]] = {}
    attempts: list[Path] = []
    for path in sorted(root.iterdir()):
        if not (path.is_dir() and path.name.startswith(SHARD_PREFIX)):
            continue
        match = ATTEMPT_DIR_RE.match(path.name)
        if match is None:
            raise ValueError(f"{path.name} names no run attempt; expected {SHARD_PREFIX}<N>-attempt-<A>")
        attempts.append(path)
        shard, attempt = int(match["shard"]), int(match["attempt"])
        if shard not in latest or attempt > latest[shard][0]:
            latest[shard] = (attempt, path)
    kept = {path for _, path in latest.values()}
    dropped = []
    for path in attempts:
        if path not in kept:
            shutil.rmtree(path)
            dropped.append(path.name)
    for shard, (_, path) in latest.items():
        path.rename(root / f"{SHARD_PREFIX}{shard}")
    return dropped


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(USAGE, file=sys.stderr)
        return EXIT_USAGE
    try:
        dropped = keep_latest_attempts(Path(argv[0]))
    except (FileNotFoundError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    for name in dropped:
        print(f"Dropped {name}: a later attempt of the same shard replaces it.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
