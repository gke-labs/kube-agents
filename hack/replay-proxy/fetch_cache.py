"""Download what recent trusted nightlies selected from the shared replay cache, for the replay lane.

The nightly uploads answers from runs that passed and, per call, a record of
the objects it confirmed in the bucket, with each one's size and, for a slot,
the answer it points at (publish_cache.py). The lane replays those nights: this
lists the records under gs://<bucket>/replay-cache/builds/ younger than
`--max-age-hours`, takes the objects they confirmed under this checkout's
normalizer version (a different version's keys never match), and downloads
them into a cache directory laid out the way replay_cache.py reads it
(variants nested again, one directory per context). It also writes the
records' runs, per build, for the lane's verdict comparison (lane_report.py).

What fits under `--max-bytes` is chosen before anything downloads, in this
order, newest record first within each kind: answers; slots whose answer was
chosen; variants; messages. An object is costed as the disk it takes, its size
rounded up to whole 4 KiB blocks plus one block for its directory entry (and a
block for each new variant directory), from the size the record gives. What
does not fit is left out and counted, never refused: the lane replays less
rather than nothing. After the download each file is costed again at its real
size (an object the nightly found already in the bucket can differ from the
copy it recorded), and one past the bound is removed and counted. A slot whose
answer did not arrive (left out, expired, or never written) is removed too, so
no pointer names an answer the pod lacks.

Only the nightly runner can create objects in the bucket (docs/ci-pool-projects.md,
"The replay cache bucket"). Object keys are still checked before anything
becomes a local path: every listed key must be a record at
`builds/<build>/publish-<ms>.json`, and every name a record confirms must be in
the cache layout (publish_cache.OBJECT_NAME_RE). A key or name that is not,
including one with an empty, `.` or `..` segment, refuses the whole download,
as the shadow bucket's recipe does (hack/replay-proxy/README.md, "In CI"): a
key outside the layout means the writer is not the code this reads.

Prints one JSON summary line; exits 1 when no answer arrived or a key was
refused, so the lane stops before it spends a cluster on nothing.
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
# A record's key under replay-cache/.
RECORD_KEY_RE = re.compile(rf"^builds/({P.BUILD_PATTERN})/publish-[0-9]{{1,16}}\.json$")
SAFE_HEX = P.SAFE_HEX
# One `gcloud storage ls -l` object line: size, creation time, URL.
LS_LINE_RE = re.compile(r"^\s*(\d+)\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?Z\s+(gs://\S+)\s*$")
LS_TOTAL_PREFIX = "TOTAL:"
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"
SECONDS_PER_HOUR = 3600
DEFAULT_MAX_AGE_HOURS = 30.0
DEFAULT_MAX_BYTES = 512 * 1024 * 1024
# The cost of an object on the pod's disk: whole blocks, and a block for its
# directory entry (spool.py counts blocks for the same reason).
DISK_BLOCK_BYTES = 4096
FILE_OVERHEAD_BYTES = DISK_BLOCK_BYTES
GCLOUD_TIMEOUT_SECONDS = 600.0
TEXT_ENCODING = "utf-8"
STAGING_PREFIX = "replay-cache-fetch-"
GCLOUD_ENV = {"CLOUDSDK_CORE_DISABLE_PROMPTS": "1"}
# The order kinds are chosen in, and the kinds' names in the summary.
PRIORITY = (P.ANSWERS, P.SLOTS, P.VARIANTS, P.MESSAGES)

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
    by_kind: Dict[str, int] = field(default_factory=dict)
    left_out: Dict[str, int] = field(default_factory=dict)
    removed: Dict[str, int] = field(default_factory=dict)
    runs: Dict[str, List[Dict[str, object]]] = field(default_factory=dict)

    def count(self, table: Dict[str, int], kind: str, n: int = 1) -> None:
        table[kind] = table.get(kind, 0) + n


def disk_cost(size: int) -> int:
    """What a file of `size` bytes takes on disk: whole blocks plus its directory entry."""
    return -(-max(size, 0) // DISK_BLOCK_BYTES) * DISK_BLOCK_BYTES + FILE_OVERHEAD_BYTES


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

    def _confirmed(self, record: Dict[str, object]) -> List[Tuple[str, int, str]]:
        """(name, bytes, slot target) of every object a record confirms at this version; Refused on any outside the layout."""
        out = []
        objects = (record.get(P.RECORD_OBJECTS) or {}) if isinstance(record.get(P.RECORD_OBJECTS), dict) else {}
        for item in objects.get(self.version) or []:
            name = item.get(P.OBJECT_NAME) if isinstance(item, dict) else None
            size = item.get(P.OBJECT_BYTES) if isinstance(item, dict) else None
            if not isinstance(name, str) or not P.OBJECT_NAME_RE.match(name):
                raise Refused("a record confirms a name outside the cache layout")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise Refused("a record gives an object no size")
            target = str(item.get(P.OBJECT_TARGET) or "")
            if name.startswith(P.SLOTS + "/") and not SAFE_HEX.match(target):
                raise Refused("a record gives a slot no answer")
            out.append((name, size, target))
        return out

    def _plan(self, confirmed: List[Tuple[str, int, str]], result: Fetched) -> Dict[str, List[Tuple[str, int, str]]]:
        """What fits under max_bytes, by kind in PRIORITY order; the rest counted as left out."""
        by_kind: Dict[str, List[Tuple[str, int, str]]] = {kind: [] for kind in PRIORITY}
        for item in confirmed:
            by_kind[item[0].partition("/")[0]].append(item)
        chosen: Dict[str, List[Tuple[str, int, str]]] = {kind: [] for kind in PRIORITY}
        answers: Set[str] = set()
        contexts: Set[str] = set()
        used = 0
        for kind in PRIORITY:
            for name, size, target in by_kind[kind]:
                if kind == P.SLOTS and target not in answers:
                    result.count(result.left_out, kind)
                    continue
                cost = disk_cost(size)
                context = name.partition("/")[2].partition(P.VARIANT_SEPARATOR)[0] if kind == P.VARIANTS else ""
                if context and context not in contexts:
                    cost += DISK_BLOCK_BYTES
                if used + cost > self.max_bytes:
                    result.count(result.left_out, kind)
                    continue
                used += cost
                if context:
                    contexts.add(context)
                if kind == P.ANSWERS:
                    answers.add(name.partition("/")[2][: -len(P.JSON_SUFFIX)])
                chosen[kind].append((name, size, target))
        return chosen

    def fetch(self, dest: str, runs_out: Optional[str] = None) -> Fetched:
        result = Fetched()
        keys = self.records()
        stage = tempfile.mkdtemp(prefix=STAGING_PREFIX)
        try:
            by_build: Dict[str, List[str]] = {}
            for key in keys:
                by_build.setdefault(RECORD_KEY_RE.match(key).group(1), []).append(key)  # type: ignore[union-attr]
            # One copy per build: a record's name is unique within its build only.
            for build, build_keys in by_build.items():
                self._download("fetch_records", [f"gs://{self.bucket}/{CACHE_ROOT}/{k}" for k in build_keys],
                               os.path.join(stage, BUILDS_DIR, build))
            confirmed: Dict[str, Tuple[str, int, str]] = {}
            # Newest first, so what the bound leaves out is the oldest.
            for key in reversed(keys):
                build = RECORD_KEY_RE.match(key).group(1)  # type: ignore[union-attr]
                record = P.read_json(os.path.join(stage, BUILDS_DIR, build, os.path.basename(key)))
                if not isinstance(record, dict):
                    continue
                result.records += 1
                if build not in result.builds:
                    result.builds.append(build)
                runs = record.get(P.RECORD_RUNS)
                if isinstance(runs, list) and len(runs) > len(result.runs.get(build, [])):
                    result.runs[build] = [r for r in runs if isinstance(r, dict)]
                for item in self._confirmed(record):
                    confirmed.setdefault(item[0], item)
            result.objects = len(confirmed)
            chosen = self._plan(list(confirmed.values()), result)
            prefix = P.cache_prefix(self.bucket, self.version)
            used = 0
            contexts: Set[str] = set()
            answers: Set[str] = set()
            for kind in PRIORITY:
                if not chosen[kind]:
                    continue
                into = os.path.join(stage, kind)
                self._download("fetch_objects", [f"{prefix}/{name}" for name, _, _ in chosen[kind]], into)
                for name, _, target in chosen[kind]:
                    got = os.path.join(into, os.path.basename(name))
                    if not os.path.isfile(got) or os.path.islink(got):
                        continue
                    if kind == P.SLOTS and (P.read_json(got) != target or target not in answers):
                        result.count(result.removed, kind)
                        continue
                    size = os.path.getsize(got)
                    cost = disk_cost(size)
                    context = name.partition("/")[2].partition(P.VARIANT_SEPARATOR)[0] if kind == P.VARIANTS else ""
                    if context and context not in contexts:
                        cost += DISK_BLOCK_BYTES
                    if used + cost > self.max_bytes:
                        result.count(result.removed, kind)
                        continue
                    used += cost
                    if context:
                        contexts.add(context)
                    target_path = object_path(dest, name)
                    os.makedirs(os.path.dirname(target_path), exist_ok=True)
                    shutil.move(got, target_path)
                    if kind == P.ANSWERS:
                        answers.add(os.path.basename(name)[: -len(P.JSON_SUFFIX)])
                    result.downloaded += 1
                    result.bytes += size
                    result.count(result.by_kind, kind)
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
                   downloaded=result.downloaded, bytes=result.bytes, fetched=result.by_kind,
                   left_out=result.left_out, removed=result.removed)
    print(json.dumps(summary, sort_keys=True))
    answers = os.path.join(args.dest, P.ANSWERS)
    return 0 if os.path.isdir(answers) and os.listdir(answers) else 1


if __name__ == "__main__":
    sys.exit(main())
