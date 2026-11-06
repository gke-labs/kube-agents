"""Download what recent trusted nightlies selected from the shared replay cache, for the replay lane.

The nightly uploads answers from runs that passed and, per call, a record of
the names it confirmed in the bucket (publish_cache.py). The lane replays
those nights: this lists the records under gs://<bucket>/replay-cache/builds/
younger than `--max-age-hours`, takes the names they confirmed under this
checkout's normalizer version (a different version's keys never match), and
downloads exactly those objects into a cache directory laid out the way
replay_cache.py reads it (variants nested again, one directory per context).
It also writes the records' runs, per build, for the lane's verdict
comparison (lane_report.py).

Only the nightly runner can create objects in the bucket (docs/ci-pool-projects.md,
"The replay cache bucket"), so everything here was written by a trusted nightly.
Object keys are still checked before anything becomes a local path: every
listed key must be a record at `builds/<build>/publish-<ms>.json`, and every
name a record confirms must be one of the four layouts below. A key or name
that is not, including one with an empty, `.` or `..` segment, refuses the
whole download, as the shadow bucket's recipe does
(hack/replay-proxy/README.md, "In CI"): a key outside the layout means the
writer is not the code this reads. The download stops at `--max-bytes`.

Prints one JSON summary line; exits 1 when there is nothing to replay or a
key was refused, so the lane stops before it spends a cluster on nothing.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

import normalizer
import publish_cache as P

CACHE_ROOT = P.CACHE_ROOT
BUILDS_DIR = P.BUILDS_DIR
RUNS_FILE = "nightly-runs.json"
HEX = P.HEX
# A record's key under replay-cache/, and the names a record may confirm.
RECORD_KEY_RE = re.compile(r"^builds/([A-Za-z0-9][A-Za-z0-9_-]*)/publish-[0-9]{1,16}\.json$")
OBJECT_NAME_RE = re.compile(rf"^(?:(answers|slots|messages)/{HEX}\.json|variants/{HEX}-{HEX}\.json)$")
# One `gcloud storage ls -l` object line: size, creation time, URL.
LS_LINE_RE = re.compile(r"^\s*(\d+)\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?Z\s+(gs://\S+)\s*$")
LS_TOTAL_PREFIX = "TOTAL:"
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"
SECONDS_PER_HOUR = 3600
DEFAULT_MAX_AGE_HOURS = 30.0
DEFAULT_MAX_BYTES = 512 * 1024 * 1024
GCLOUD_TIMEOUT_SECONDS = 600.0
TEXT_ENCODING = "utf-8"
STAGING_PREFIX = "replay-cache-fetch-"
GCLOUD_ENV = {"CLOUDSDK_CORE_DISABLE_PROMPTS": "1"}

Run = Callable[..., Optional[subprocess.CompletedProcess]]


class Refused(Exception):
    """A key or name outside the layout: the download stops whole."""


@dataclass
class Fetched:
    records: int = 0
    builds: List[str] = field(default_factory=list)
    objects: int = 0
    downloaded: int = 0
    bytes: int = 0
    runs: Dict[str, List[Dict[str, object]]] = field(default_factory=dict)


def parse_listing(text: str, bucket: str) -> List[Tuple[int, float, str]]:
    """(size, created epoch, key under replay-cache/) of every listed object; Refused on anything else."""
    prefix = f"gs://{bucket}/{CACHE_ROOT}/"
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(LS_TOTAL_PREFIX):
            continue
        match = LS_LINE_RE.match(raw)
        if not match or not match.group(3).startswith(prefix):
            raise Refused("a listed line is not an object of the cache bucket")
        created = calendar.timegm(time.strptime(match.group(2), TIME_FORMAT))
        out.append((int(match.group(1)), float(created), match.group(3)[len(prefix):]))
    return out


def object_path(dest: str, name: str) -> str:
    """Where replay_cache.py reads an object: variants nested one directory per context."""
    sub, _, base = name.partition("/")
    if sub == P.VARIANTS:
        context, _, answer = base.partition(P.VARIANT_SEPARATOR)
        return os.path.join(dest, sub, context, answer)
    return os.path.join(dest, sub, base)


class Fetcher:
    def __init__(self, *, bucket: str, version: str, run: Run, max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
                 max_bytes: int = DEFAULT_MAX_BYTES, clock: Callable[[], float] = time.time):
        self.bucket = bucket
        self.version = version
        self.run = run
        self.max_age_seconds = max_age_hours * SECONDS_PER_HOUR
        self.max_bytes = max_bytes
        self.clock = clock

    def _gcloud(self, kind: str, args: Sequence[str], input: Optional[bytes] = None) -> Optional[subprocess.CompletedProcess]:
        return self.run(kind, ["gcloud", "storage", *args], GCLOUD_TIMEOUT_SECONDS, input=input, env=GCLOUD_ENV)

    def records(self) -> List[str]:
        """Keys of the records young enough to replay, oldest first."""
        proc = self._gcloud("list_records", ["ls", "-l", f"gs://{self.bucket}/{CACHE_ROOT}/{BUILDS_DIR}/**"])
        if proc is None:
            return []
        listed = parse_listing(proc.stdout.decode(TEXT_ENCODING, "replace"), self.bucket)
        for _, _, key in listed:
            if not RECORD_KEY_RE.match(key):
                raise Refused("a key under builds/ is not a publish record")
        oldest = self.clock() - self.max_age_seconds
        return [key for _, created, key in sorted(listed, key=lambda item: (item[1], item[2])) if created >= oldest]

    def _download(self, kind: str, urls: Sequence[str], into: str) -> None:
        os.makedirs(into, exist_ok=True)
        # --continue-on-error: an object a lifecycle rule has expired since its
        # record was written is skipped, not the end of the download.
        self._gcloud(kind, ["cp", "--continue-on-error", "-I", into + os.sep],
                     input=("\n".join(urls) + "\n").encode())

    def fetch(self, dest: str, runs_out: Optional[str] = None) -> Fetched:
        result = Fetched()
        keys = self.records()
        stage = tempfile.mkdtemp(prefix=STAGING_PREFIX)
        try:
            names: Set[str] = set()
            by_build: Dict[str, List[str]] = {}
            for key in keys:
                by_build.setdefault(RECORD_KEY_RE.match(key).group(1), []).append(key)  # type: ignore[union-attr]
            # One copy per build: a record's name is unique within its build only.
            for build, build_keys in by_build.items():
                self._download("fetch_records", [f"gs://{self.bucket}/{CACHE_ROOT}/{k}" for k in build_keys],
                               os.path.join(stage, BUILDS_DIR, build))
            for key in keys:
                build = RECORD_KEY_RE.match(key).group(1)  # type: ignore[union-attr]
                record = P.read_json(os.path.join(stage, BUILDS_DIR, build, os.path.basename(key)))
                if not isinstance(record, dict):
                    continue
                result.records += 1
                if build not in result.builds:
                    result.builds.append(build)
                runs = record.get("runs")
                if isinstance(runs, list) and len(runs) >= len(result.runs.get(build, [])):
                    result.runs[build] = [r for r in runs if isinstance(r, dict)]
                confirmed = (record.get("objects") or {}).get(self.version) or []
                for name in confirmed:
                    if not isinstance(name, str) or not OBJECT_NAME_RE.match(name):
                        raise Refused("a record confirms a name outside the cache layout")
                    names.add(name)
            result.objects = len(names)
            by_sub: Dict[str, List[str]] = {}
            for name in sorted(names):
                by_sub.setdefault(name.partition("/")[0], []).append(name)
            prefix = P.cache_prefix(self.bucket, self.version)
            for sub in P.UPLOAD_ORDER:
                wanted = by_sub.get(sub, [])
                if not wanted:
                    continue
                into = os.path.join(stage, sub)
                self._download("fetch_objects", [f"{prefix}/{n}" for n in wanted], into)
                for name in wanted:
                    got = os.path.join(into, os.path.basename(name))
                    if not os.path.isfile(got) or os.path.islink(got):
                        continue
                    result.bytes += os.path.getsize(got)
                    if result.bytes > self.max_bytes:
                        raise Refused(f"the selected answers pass the {self.max_bytes}-byte bound")
                    target = object_path(dest, name)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    shutil.move(got, target)
                    result.downloaded += 1
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        if runs_out:
            with open(runs_out, "w", encoding=TEXT_ENCODING) as handle:
                json.dump(result.runs, handle, sort_keys=True)
        return result


def default_run(kind: str, cmd: Sequence[str], timeout: float, input: Optional[bytes] = None,
                env: Optional[Dict[str, str]] = None) -> Optional[subprocess.CompletedProcess]:
    merged = dict(os.environ)
    merged.update(env or {})
    try:
        proc = subprocess.run(list(cmd), input=input, capture_output=True, timeout=timeout, env=merged,
                              stdin=None if input is not None else subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError):
        return None
    # `cp --continue-on-error` exits non-zero when it skipped an object; what
    # arrived is counted by the caller, so only a listing's failure is fatal.
    if proc.returncode != 0 and kind not in ("fetch_records", "fetch_objects"):
        return None
    return proc


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--dest", required=True, help="the cache directory to fill (replay_cache.py's layout)")
    parser.add_argument("--runs-out", default="", help=f"where to write the records' runs per build ({RUNS_FILE})")
    parser.add_argument("--version", default=normalizer.NORMALIZER_VERSION,
                        help="the normalizer version to fetch (default: this checkout's)")
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    args = parser.parse_args(argv)
    summary: Dict[str, object] = {"event": "fetch", "version": args.version}
    if not P.valid_bucket(args.bucket) or not P.NORMALIZER_VERSION_RE.match(args.version):
        summary["error"] = "bad_arguments"
        print(json.dumps(summary, sort_keys=True))
        return 1
    fetcher = Fetcher(bucket=args.bucket, version=args.version, run=default_run,
                      max_age_hours=args.max_age_hours, max_bytes=args.max_bytes)
    try:
        result = fetcher.fetch(args.dest, args.runs_out or None)
    except Refused as exc:
        summary["error"] = "refused"
        summary["reason"] = str(exc)
        print(json.dumps(summary, sort_keys=True))
        return 1
    summary.update(records=result.records, builds=len(result.builds), objects=result.objects,
                   downloaded=result.downloaded, bytes=result.bytes)
    print(json.dumps(summary, sort_keys=True))
    answers = os.path.join(args.dest, P.ANSWERS)
    return 0 if os.path.isdir(answers) and os.listdir(answers) else 1


if __name__ == "__main__":
    sys.exit(main())
