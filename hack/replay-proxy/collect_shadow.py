"""Copy the shadow proxy's lines and stored text to a private bucket while the eval runs.

hack/ci-eval-pr.sh starts this in the background when EVAL_REPLAY_SHADOW=true
(hack/ci-env.sh, start_replay_shadow_collector) and stops it from its EXIT
trap. Every `--interval` seconds it:

1. lists the proxy pods (`--selector`) whose proxy container is running,
   terminating ones included, since a pod being replaced still has files;
2. lists, inside each pod, the files under the data volume that are complete
   and not yet empty: closed log segments (`logs/*.jsonl`, spool.py) and the
   store's `store/requests/*.json` and `store/blobs/*.json`. Open segments and
   the store's `*.tmp` files are still being written and are left for later;
3. copies the files it has not uploaded yet out of the pod as one tar stream
   through `kubectl exec` (the proxy image has python3 and nothing else this
   needs), into a staging directory outside the job's artifacts;
4. uploads them with `gcloud storage cp --no-clobber` under
   gs://<bucket>/replay-shadow/<kind>/<YYYY-MM-DD>/<build>/, keeping the
   logs/, store/requests/ and store/blobs/ layout, and deletes the local copy;
5. empties, in the pod, each file it has uploaded (and each file whose name it
   uploaded from an earlier pod, which for a content-addressed blob is the same
   content), so the data volume holds only what is not yet in the bucket. An
   empty file stays behind, so the store never writes that blob again. A file
   is emptied only after `gcloud storage cp` exited 0 for the copy that held
   it, and only while it still has the size that was uploaded; an open
   segment or a temporary file is never emptied.

What the bucket holds is never lost to a proxy restart, and what the pod holds
is at most one interval of lines plus whatever an upload failure left behind.
On SIGTERM, or when `--parent-pid` exits, it makes one last pass that also
takes the open segments (cut back to their last whole line), bounded by
`--final-deadline` seconds, and exits.

Every name the store and the segment log write is written once: a blob is
named for the hash of its content, a request manifest for a random request id,
a segment for the pod, the proxy's start time and a sequence number. So an
object already in the bucket under a name holds what the pod's file holds, and
`--no-clobber` skipping it loses nothing.

The proxy's mode adds to that (`--mode`; examples/inference-replay/eval-design.md,
Rollout step 2):

- `record`, on a trusted nightly: each pass also copies the answers the proxy
  saved (the cache directory, replay_cache.py's layout) out of the pod into
  `--keep-dir` on this machine, empties each copied file in the pod (an empty
  file still holds its name, so the first write for a key still wins), and
  keeps a copy of every line and stored request it fetched. Every
  `--publish-interval` seconds, and once in the last pass, publish_cache.py
  uploads the answers from runs that passed, as graded so far under
  `--verdicts-dir`, to the shared cache bucket. The cache never goes to the
  shadow bucket.
- `replay`, in the replay lane: each pass seeds any proxy pod that has not got
  the cache (a pod that replaced one starts empty) from `--seed-dir`, which
  fetch_cache.py filled, and keeps the lines and stored requests. The last
  pass writes lane_report.py's report and uploads it under the job's prefix,
  in `report/`. `--seed-once` seeds every pod and exits, 0 only when every
  pod took the cache, so the lane can stop before its cases. A pod being
  deleted is never seeded, though its lines are still collected.

It never fails the eval: every command has a timeout, an error is counted and
retried on the next pass, and the exit status is always 0. The job's stdout
and the artifacts are public through Prow's web UI, so what this prints is
counts and error classes (never a command's own error text, which can name a
file or the bucket), with any long hex string masked as well; file names and
file contents never reach either.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional, Sequence, Set, Tuple

import replay_cache

BUCKET_ROOT = "replay-shadow"
# One folder each, so an IAM condition on the object name can confine each
# runner to its own: nightly/ is written by the nightly runner alone, and a
# scheduled run as any other account is nightly-untrusted (hack/ci-env.sh,
# resolve_replay_shadow). The replay lane is its own folder too.
JOB_KINDS = ("nightly", "nightly-untrusted", "presubmit", "replay-lane")
MODE_SHADOW = "shadow"
MODE_RECORD = "record"
MODE_REPLAY = "replay"
MODES = (MODE_SHADOW, MODE_RECORD, MODE_REPLAY)
DATE_FORMAT = "%Y-%m-%d"
JOB_MANIFEST_PREFIX = "job-"
JSON_SUFFIX = ".json"
SEGMENT_SUFFIX = ".jsonl"
OPEN_SUFFIX = ".open"
LOGS_DIR = "logs"
STORE_DIR = "store"
STORE_REQUESTS_DIR = "store/requests"
STORE_BLOBS_DIR = "store/blobs"
# What a pod's data volume may hold that is ours to copy, by directory, and the
# suffix a complete file there has.
SOURCES = ((LOGS_DIR, SEGMENT_SUFFIX), (STORE_REQUESTS_DIR, JSON_SUFFIX), (STORE_BLOBS_DIR, JSON_SUFFIX))
# Every name the pod may hand back, by the same rule. The listing and the tar
# stream come from the proxy's container, whose image is built from the commit
# under test, so a name is checked here before it becomes a path on this
# machine or an object name: one directory from SOURCES, one plain file name.
SAFE_NAME = re.compile(
    r"^(?:logs/[A-Za-z0-9][A-Za-z0-9._-]*\.jsonl(?:\.open)?"
    r"|store/(?:requests|blobs)/[A-Za-z0-9][A-Za-z0-9._-]*\.json)$"
)

# The proxy's saved answers, under the cache directory in the data volume
# (replay_cache.py): what record mode copies out and replay mode seeds. Every
# file is one plain name under one of four directories, variants one level
# deeper, so a name is checked like the others before it becomes a path.
DEFAULT_CACHE_SUBDIR = "replay-cache"
CACHE_SUBDIR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
CACHE_REL_RE = replay_cache.CACHE_REL_RE
CACHE_SEED_MARKER = ".seeded"
# The kept copy on this machine: one directory per pod (its lines and its
# saved answers), and the store, whose names are unique across pods.
KEEP_PODS_DIR = "pods"
KEEP_LOGS_DIR = "logs"
KEEP_CACHE_DIR = "cache"
POD_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")
DEFAULT_PUBLISH_INTERVAL_SECONDS = 300.0
PUBLISH_THREAD_NAME = "replay-cache-publish"
# A file being written: the store's, and the collector's own kept copies.
TMP_SUFFIX = ".tmp"
# Stands for TMP_SUFFIX in the in-pod scripts' text, filled in where they are defined.
TMP_SUFFIX_PLACEHOLDER = "__TMP_SUFFIX__"
SEED_TAR_NAME = "seed.tar"
REPORT_DIR = "report"

# The chart closes a segment 30 s after its first line
# (REPLAY_LOG_SEGMENT_SECONDS), so at this interval a line is in the bucket
# about a minute after the proxy wrote it.
DEFAULT_INTERVAL_SECONDS = 30.0
DEFAULT_FINAL_DEADLINE_SECONDS = 60.0
DEFAULT_DATA_DIR = "/data"
STAGING_PREFIX = "replay-shadow-"
SLEEP_SLICE_SECONDS = 1.0
KUBECTL_TIMEOUT_SECONDS = 120.0
GCLOUD_TIMEOUT_SECONDS = 180.0
# One exec's tar stream is held in memory; one pass copies at most this much,
# so a backlog drains over several passes and no pass outlives the deadline.
BATCH_MAX_BYTES = 64 * 1024 * 1024
PASS_MAX_BYTES = 512 * 1024 * 1024
# Error events printed per step and class; the per-pass report keeps counting.
MAX_ERROR_EVENTS_PER_KIND = 5
MS_PER_SECOND = 1000
POD_PHASE_RUNNING = "Running"
# Set on a pod being deleted. Such a pod keeps running through the chart's
# preStop drain, and can go at any moment of it.
POD_DELETION_FIELD = "deletionTimestamp"
NUL = b"\0"
NEWLINE = b"\n"
# Long hex runs: request ids, blob names, fingerprints. Masked in anything printed.
HEX_RUN = re.compile(r"[0-9a-fA-F]{32,}")
HEX_MASK = "<hex>"
# A failed command is reported by the first of these its stderr matches, never
# by the text itself.
ERROR_CLASSES = (
    ("permission_denied", re.compile(r"403|AccessDenied|Forbidden|PERMISSION_DENIED|does not have .*permission", re.I)),
    ("unauthenticated", re.compile(r"401|Unauthorized|UNAUTHENTICATED|credentials|reauthenticat", re.I)),
    ("not_found", re.compile(r"404|NotFound|not found|No such", re.I)),
    ("rate_limited", re.compile(r"429|rateLimit|Too Many Requests|RESOURCE_EXHAUSTED", re.I)),
    ("server_error", re.compile(r"\b5\d\d\b|InternalError|ServiceUnavailable|backendError", re.I)),
    ("exec_failed", re.compile(r"container not found|is not valid for pod|unable to upgrade|error dialing|exec failed", re.I)),
    ("network", re.compile(r"connection refused|connection reset|i/o timeout|no route|EOF", re.I)),
)
ERROR_CLASS_OTHER = "other"
# The shapes the bucket and build must have (hack/ci-env.sh's REPLAY_SHADOW_BUCKET_RE
# and REPLAY_SHADOW_BUILD_RE), so no object key this writes holds a `.` or `..`
# segment: the bucket's IAM conditions match a key's prefix as text, and a
# downloader that resolved `presubmit/x/../../nightly/` would file it elsewhere.
BUCKET_SHAPE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,61}[a-z0-9]$")
BUILD_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
GCLOUD_ENV = {
    # A composite upload writes and deletes temporary objects, which needs a
    # delete grant the presubmit's identity should not hold.
    "CLOUDSDK_STORAGE_PARALLEL_COMPOSITE_UPLOAD_ENABLED": "False",
    "CLOUDSDK_CORE_DISABLE_PROMPTS": "1",
}

# Run inside the proxy container as `python3 -c <script> <data dir> <0|1>`:
# prints [[relative path, size], ...] for the complete, non-empty files, and
# with 1 also the open segments.
LIST_SCRIPT = r"""
import json, os, sys
root, with_open = sys.argv[1], sys.argv[2] == "1"
sources = json.loads(sys.argv[3])
out = []
for sub, suffix in sources:
    base = os.path.join(root, sub)
    try:
        names = os.listdir(base)
    except OSError:
        continue
    for name in names:
        ok = name.endswith(suffix) or (with_open and suffix == ".jsonl" and name.endswith(".jsonl.open"))
        if not ok:
            continue
        try:
            st = os.lstat(os.path.join(base, name))
        except OSError:
            continue
        if st.st_size > 0 and (st.st_mode & 0o170000) == 0o100000:
            out.append([sub + "/" + name, st.st_size])
print(json.dumps(out))
"""

# `python3 -c <script> <data dir>`, NUL-separated relative paths on stdin: a tar
# stream of those files on stdout, each cut at the size it had when opened. A
# path outside the data directory, or one that has gone, is skipped.
TAR_SCRIPT = r"""
import os, sys, tarfile
root = os.path.realpath(sys.argv[1])
paths = [p for p in sys.stdin.buffer.read().decode().split("\0") if p]
with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as tar:
    for rel in paths:
        full = os.path.realpath(os.path.join(root, rel))
        if not full.startswith(root + os.sep):
            continue
        try:
            handle = open(full, "rb")
        except OSError:
            # An open segment the proxy closed since it was listed: read it
            # under its closed name (still sent as the listed name).
            if not full.endswith(".jsonl.open"):
                continue
            try:
                handle = open(full[: -len(".open")], "rb")
            except OSError:
                continue
        with handle:
            info = tarfile.TarInfo(rel)
            info.size = os.fstat(handle.fileno()).st_size
            tar.addfile(info, handle)
"""

# `python3 -c <script> <data dir>`, NUL-separated `<size>\t<relative path>`
# entries on stdin: empties each complete file that still has that size (never
# an open segment or a temporary file); prints how many.
EMPTY_SCRIPT = r"""
import os, sys
root = os.path.realpath(sys.argv[1])
entries = [e for e in sys.stdin.buffer.read().decode().split("\0") if e]
emptied = 0
for entry in entries:
    size, _, rel = entry.partition("\t")
    if not rel or rel.endswith(".open") or rel.endswith("__TMP_SUFFIX__"):
        continue
    full = os.path.realpath(os.path.join(root, rel))
    if not full.startswith(root + os.sep):
        continue
    try:
        if os.stat(full).st_size != int(size):
            continue
        os.truncate(full, 0)
        emptied += 1
    except (OSError, ValueError):
        pass
print(emptied)
""".replace(TMP_SUFFIX_PLACEHOLDER, TMP_SUFFIX)


# `python3 -c <script> <data dir> <cache subdir> <rel regex>`: prints
# [[relative path, size], ...] for every saved-answer file, empty ones too (a
# variant is an empty file), skipping temporary files.
CACHE_LIST_SCRIPT = r"""
import json, os, re, sys
root, sub, rel_re = sys.argv[1], sys.argv[2], re.compile(sys.argv[3])
base = os.path.join(root, sub)
out = []
for dirpath, dirnames, names in os.walk(base):
    dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
    for name in names:
        full = os.path.join(dirpath, name)
        rel = os.path.relpath(full, base).replace(os.sep, "/")
        if not rel_re.match(rel):
            continue
        try:
            st = os.lstat(full)
        except OSError:
            continue
        if (st.st_mode & 0o170000) == 0o100000:
            out.append([sub + "/" + rel, st.st_size])
print(json.dumps(out))
"""

# `python3 -c <script> <data dir> <cache subdir> <marker>`: prints 1 when the
# cache has been seeded (the marker is there), else 0.
SEEDED_SCRIPT = r"""
import os, sys
print(1 if os.path.isfile(os.path.join(sys.argv[1], sys.argv[2], sys.argv[3])) else 0)
"""

# `python3 -c <script> <data dir> <cache subdir> <marker> <rel regex>`, a tar
# stream on stdin: writes each regular member whose name fits the cache layout
# into the cache directory (atomically, so the proxy never reads half a file),
# then the marker, which tells the proxy to rebuild its miss index; prints how
# many were written. Any other member is skipped.
SEED_SCRIPT = r"""
import os, re, sys, tarfile
root, sub, marker, rel_re = sys.argv[1], sys.argv[2], sys.argv[3], re.compile(sys.argv[4])
base = os.path.join(root, sub)
written = 0
with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as tar:
    for member in tar:
        if not member.isfile() or not rel_re.match(member.name):
            continue
        handle = tar.extractfile(member)
        if handle is None:
            continue
        path = os.path.join(base, member.name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".seed.tmp"
        with open(tmp, "wb") as out:
            out.write(handle.read())
        os.replace(tmp, path)
        written += 1
mark = os.path.join(base, marker)
with open(mark + ".tmp", "w") as out:
    out.write(str(written))
os.replace(mark + ".tmp", mark)
print(written)
"""


def _copy_atomically(src: str, target: str) -> None:
    """Copy so a reader (the publisher's thread) never sees half a file: a temporary name, then a rename."""
    tmp = f"{target}.{os.getpid()}.{threading.get_ident()}{TMP_SUFFIX}"
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def mask(text: str) -> str:
    return HEX_RUN.sub(HEX_MASK, text)


def error_class(stderr: bytes) -> str:
    """What kind of failure a command's stderr describes, without repeating any of it."""
    text = (stderr or b"").decode("utf-8", "replace")
    for name, pattern in ERROR_CLASSES:
        if pattern.search(text):
            return name
    return ERROR_CLASS_OTHER


def object_name(rel: str) -> str:
    """Where a pod's file goes under the job's prefix: an open segment under its closed name."""
    return rel[: -len(OPEN_SUFFIX)] if rel.endswith(OPEN_SUFFIX) else rel


def job_prefix(bucket: str, kind: str, day: str, build: str) -> str:
    return f"gs://{bucket}/{BUCKET_ROOT}/{kind}/{day}/{build}"


def batches(files: Sequence[Tuple[str, int]], limit: int) -> List[List[Tuple[str, int]]]:
    """Consecutive groups of at most `limit` bytes; a larger file goes alone."""
    out: List[List[Tuple[str, int]]] = []
    current: List[Tuple[str, int]] = []
    size = 0
    for item in files:
        if current and size + item[1] > limit:
            out.append(current)
            current, size = [], 0
        current.append(item)
        size += item[1]
    if current:
        out.append(current)
    return out


def whole_lines(data: bytes) -> bytes:
    """`data` up to and including its last newline."""
    return data[: data.rfind(NEWLINE) + 1]


Runner = Callable[..., subprocess.CompletedProcess]


def default_runner(cmd: Sequence[str], input: Optional[bytes] = None,
                   remaining: Callable[[], float] = lambda: float("inf"),
                   env: Optional[Dict[str, str]] = None,
                   stdin_path: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run `cmd`, killing it once `remaining()` reaches 0.

    `remaining` is asked again every SLEEP_SLICE_SECONDS rather than once, so a
    SIGTERM that brings the collector's deadline forward also cuts a command
    already running. `stdin_path` feeds a file instead of `input`, for a stream
    too large to hold in memory (a seeded cache).
    """
    merged = dict(os.environ)
    merged.update(env or {})
    if stdin_path is not None:
        with open(stdin_path, "rb") as stdin:
            proc = subprocess.Popen(list(cmd), stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=merged)
    else:
        proc = subprocess.Popen(list(cmd), stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=merged)
    pending = input
    while True:
        left = remaining()
        if left <= 0:
            proc.kill()
            proc.communicate()
            raise subprocess.TimeoutExpired(list(cmd), 0)
        try:
            # Input goes on the first call only; Popen refuses it on a retry
            # and keeps feeding what it has.
            out, err = proc.communicate(pending, timeout=min(left, SLEEP_SLICE_SECONDS))
        except subprocess.TimeoutExpired:
            pending = None
            continue
        return subprocess.CompletedProcess(list(cmd), proc.returncode, out, err)


class ProxyPod(NamedTuple):
    name: str
    # Being deleted: its lines are still collected (the drain exists for them),
    # but it is never seeded, since it can vanish mid-copy and will serve nothing.
    draining: bool


@dataclass
class Totals:
    passes: int = 0
    uploaded_files: int = 0
    uploaded_bytes: int = 0
    emptied: int = 0
    kept_cache: int = 0
    seeded: int = 0
    errors: Dict[str, int] = field(default_factory=dict)

    def error(self, kind: str) -> int:
        """Count one error of `kind`; returns how many there have been."""
        self.errors[kind] = self.errors.get(kind, 0) + 1
        return self.errors[kind]


class Collector:
    def __init__(
        self, *, namespace: str, selector: str, container: str, dest: str, staging: str,
        context: str = "", data_dir: str = DEFAULT_DATA_DIR, runner: Runner = default_runner,
        out=None, clock: Callable[[], float] = time.monotonic, mode: str = MODE_SHADOW,
        keep_dir: str = "", cache_subdir: str = DEFAULT_CACHE_SUBDIR, seed_dir: str = "",
        publisher=None, publish_interval: float = DEFAULT_PUBLISH_INTERVAL_SECONDS,
        lane_report: Optional[Callable[[str], Optional[str]]] = None,
    ):
        self.kubectl = ["kubectl"] + (["--context", context] if context else []) + ["-n", namespace]
        self.selector = selector
        self.container = container
        self.dest = dest.rstrip("/")
        self.staging = staging
        self.data_dir = data_dir
        self.runner = runner
        self.out = out if out is not None else sys.stdout
        self.clock = clock
        self.uploaded: Set[str] = set()
        self.totals = Totals()
        # Set when the last pass begins; every command after it is cut to fit.
        self.deadline: Optional[float] = None
        self.mode = mode
        self.keep_dir = keep_dir
        self.cache_subdir = cache_subdir
        self.seed_dir = seed_dir
        self.seed_tar: Optional[str] = None
        # (pod, path in the pod) of every saved answer copied out already.
        self.kept_cache: Set[Tuple[str, str]] = set()
        self.publisher = publisher
        self.publish_interval = publish_interval
        self.last_publish: Optional[float] = None
        # Ordinary publishes run beside collection, so emptying the pods never
        # waits on one; the last pass waits for any still running, then publishes.
        self.publish_in_background = True
        self.publish_thread: Optional[threading.Thread] = None
        # Writes the lane's report into a directory and returns its summary.
        self.lane_report = lane_report

    # --- plumbing ---------------------------------------------------------

    def emit(self, record: Dict[str, object]) -> None:
        try:
            self.out.write(mask(json.dumps(record, sort_keys=True)) + "\n")
            self.out.flush()
        except (OSError, ValueError):
            pass

    def _remaining(self, started: float, default: float) -> float:
        left = default - (self.clock() - started)
        if self.deadline is not None:
            left = min(left, self.deadline - self.clock())
        return left

    def past_deadline(self) -> bool:
        return self.deadline is not None and self.clock() >= self.deadline

    def cut_short(self, final: bool) -> bool:
        """Whether a pass should stop: past the deadline, or an ordinary pass once the last one is due."""
        return self.past_deadline() or (not final and self.deadline is not None)

    def fail(self, kind: str, **detail: object) -> None:
        """Count an error and print it, up to MAX_ERROR_EVENTS_PER_KIND times per kind."""
        if self.totals.error(kind) <= MAX_ERROR_EVENTS_PER_KIND:
            self.emit({"event": "error", "kind": kind, **detail})

    def run(self, kind: str, cmd: Sequence[str], default_timeout: float, input: Optional[bytes] = None,
            env: Optional[Dict[str, str]] = None, stdin_path: Optional[str] = None) -> Optional[subprocess.CompletedProcess]:
        """Run one command; None (and an error counted and printed by class) when it fails."""
        started = self.clock()
        if self._remaining(started, default_timeout) <= 0:
            self.fail(f"{kind}_deadline")
            return None
        extra = {"stdin_path": stdin_path} if stdin_path is not None else {}
        try:
            proc = self.runner(cmd, input=input, remaining=lambda: self._remaining(started, default_timeout), env=env,
                               **extra)
        except subprocess.TimeoutExpired:
            self.fail(f"{kind}_timeout")
            return None
        except OSError as exc:
            self.fail(f"{kind}_{type(exc).__name__}")
            return None
        if proc.returncode != 0:
            self.fail(f"{kind}_{error_class(proc.stderr)}", exit=proc.returncode)
            return None
        return proc

    def exec_in(self, kind: str, pod: str, script: str, args: Sequence[str],
                stdin: Optional[bytes] = None, stdin_path: Optional[str] = None) -> Optional[subprocess.CompletedProcess]:
        piped = stdin is not None or stdin_path is not None
        cmd = self.kubectl + ["exec"] + (["-i"] if piped else []) + [
            pod, "-c", self.container, "--", "python3", "-c", script, *args]
        return self.run(kind, cmd, KUBECTL_TIMEOUT_SECONDS, input=stdin, stdin_path=stdin_path)

    # --- steps ------------------------------------------------------------

    def pods(self) -> List[ProxyPod]:
        """Every proxy pod whose container is running, draining ones included, by name."""
        proc = self.run("list_pods", self.kubectl + ["get", "pods", "-l", self.selector, "-o", "json"],
                        KUBECTL_TIMEOUT_SECONDS)
        if proc is None:
            return []
        found: Dict[str, bool] = {}
        try:
            for pod in json.loads(proc.stdout).get("items") or []:
                status = pod.get("status") or {}
                if status.get("phase") != POD_PHASE_RUNNING:
                    continue
                for cs in status.get("containerStatuses") or []:
                    if cs.get("name") == self.container and (cs.get("state") or {}).get("running") is not None:
                        metadata = pod["metadata"]
                        found[str(metadata["name"])] = bool(metadata.get(POD_DELETION_FIELD))
        except (ValueError, AttributeError, KeyError, TypeError):
            self.fail("list_pods_parse")
            return []
        return [ProxyPod(name, found[name]) for name in sorted(found)]

    def list_files(self, pod: str, with_open: bool) -> List[Tuple[str, int]]:
        proc = self.exec_in("list_files", pod, LIST_SCRIPT,
                            [self.data_dir, "1" if with_open else "0", json.dumps(SOURCES)])
        if proc is None:
            return []
        try:
            listed = [(str(rel), int(size)) for rel, size in json.loads(proc.stdout)]
        except (ValueError, TypeError):
            self.fail("list_files_parse")
            return []
        safe = [(rel, size) for rel, size in listed if SAFE_NAME.match(rel) and size > 0]
        if len(safe) != len(listed):
            self.fail("list_files_unsafe_name")
        return sorted(safe)

    def fetch(self, pod: str, batch: Sequence[Tuple[str, int]], root: str,
              safe: Callable[[str], bool] = lambda name: bool(SAFE_NAME.match(name)),
              keep_empty: bool = False) -> Dict[str, int]:
        """Copy `batch` out of the pod into `root`; returns {object name: bytes written}.

        A member the stream cut short is not written: tarfile raises reading
        it, and only members read whole reach the disk. `keep_empty` writes an
        empty member too (a saved answer's variant is an empty file).
        """
        wanted = {rel for rel, _ in batch}
        proc = self.exec_in("fetch", pod, TAR_SCRIPT, [self.data_dir], stdin=NUL.join(r.encode() for r in sorted(wanted)))
        if proc is None:
            return {}
        written: Dict[str, int] = {}
        try:
            with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r|") as tar:
                for member in tar:
                    if member.name not in wanted or not member.isfile() or not safe(member.name):
                        continue
                    handle = tar.extractfile(member)
                    data = handle.read() if handle is not None else b""
                    if member.name.endswith(OPEN_SUFFIX):
                        data = whole_lines(data)
                    if not data and not keep_empty:
                        continue
                    name = object_name(member.name)
                    path = os.path.join(root, name)
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "wb") as out:
                        out.write(data)
                    written[name] = len(data)
        except (tarfile.TarError, OSError, EOFError):
            self.fail("fetch_unpack")
        return written

    def upload(self, root: str, names: Iterable[str]) -> List[str]:
        """Upload what was fetched, one copy per destination directory; returns the names uploaded.

        A name is returned only when the `gcloud storage cp` that carried it
        exited 0. A copy that fails partway returns none of its names: some
        may be in the bucket already, and the next pass's `--no-clobber` copy
        skips those and uploads the rest.
        """
        done: List[str] = []
        groups: Dict[str, List[str]] = {}
        for name in names:
            groups.setdefault(os.path.dirname(name), []).append(name)
        for sub, members in sorted(groups.items()):
            paths = "\n".join(os.path.join(root, n) for n in sorted(members)).encode() + NEWLINE
            cmd = ["gcloud", "storage", "cp", "--no-clobber", "-I", f"{self.dest}/{sub}/"]
            if self.run("upload", cmd, GCLOUD_TIMEOUT_SECONDS, input=paths, env=GCLOUD_ENV) is None:
                continue
            for name in sorted(members):
                done.append(name)
                self.totals.uploaded_bytes += os.path.getsize(os.path.join(root, name))
        return done

    def empty(self, pod: str, files: Iterable[Tuple[str, int]]) -> None:
        """Empty `files` ([relative path, size]) in the pod; each only while it still has that size."""
        entries = [f"{size}\t{rel}".encode() for rel, size in files if not rel.endswith(OPEN_SUFFIX)]
        if not entries:
            return
        proc = self.exec_in("empty", pod, EMPTY_SCRIPT, [self.data_dir], stdin=NUL.join(entries))
        if proc is not None:
            try:
                self.totals.emptied += int(proc.stdout.strip() or 0)
            except ValueError:
                self.fail("empty_parse")

    # --- record and replay modes -----------------------------------------

    def keep(self, pod: str, root: str, written: Dict[str, int]) -> None:
        """Copy fetched lines (per pod) and stored requests into the kept copy on this machine."""
        if not self.keep_dir or not POD_NAME_RE.match(pod):
            return
        for name in written:
            if name.startswith(LOGS_DIR + "/"):
                target = os.path.join(self.keep_dir, KEEP_PODS_DIR, pod, name)
            else:
                target = os.path.join(self.keep_dir, name)
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                _copy_atomically(os.path.join(root, name), target)
            except OSError:
                self.fail("keep_write")

    def cache_safe(self, name: str) -> bool:
        prefix = self.cache_subdir + "/"
        return name.startswith(prefix) and bool(CACHE_REL_RE.match(name[len(prefix):]))

    def collect_cache(self, pod: str, budget: int, final: bool) -> int:
        """Record mode: copy new saved answers out of the pod into the kept copy, then empty them there.

        Returns the bytes copied. A file is emptied only once it is on this
        machine, and an emptied file keeps its name, so the proxy still sees
        its key as written and never saves a second answer under it.
        """
        if not POD_NAME_RE.match(pod):
            self.fail("cache_pod_name")
            return 0
        proc = self.exec_in("list_cache", pod, CACHE_LIST_SCRIPT, [self.data_dir, self.cache_subdir, CACHE_REL_RE.pattern])
        if proc is None:
            return 0
        try:
            listed = [(str(rel), int(size)) for rel, size in json.loads(proc.stdout)]
        except (ValueError, TypeError):
            self.fail("list_cache_parse")
            return 0
        files = sorted((rel, size) for rel, size in listed if self.cache_safe(rel))
        if len(files) != len(listed):
            self.fail("list_cache_unsafe_name")
        new = [(rel, size) for rel, size in files if (pod, rel) not in self.kept_cache]
        emptiable = [(rel, size) for rel, size in files if (pod, rel) in self.kept_cache and size > 0]
        copied = 0
        for batch in batches(new, BATCH_MAX_BYTES):
            if copied >= budget or self.cut_short(final):
                break
            copied += sum(size for _, size in batch)
            root = tempfile.mkdtemp(dir=self.staging)
            try:
                written = self.fetch(pod, batch, root, safe=self.cache_safe, keep_empty=True)
                for rel, _ in batch:
                    if rel not in written:
                        continue
                    target = os.path.join(self.keep_dir, KEEP_PODS_DIR, pod, KEEP_CACHE_DIR,
                                          rel[len(self.cache_subdir) + 1:])
                    try:
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        _copy_atomically(os.path.join(root, rel), target)
                    except OSError:
                        self.fail("keep_write")
                        continue
                    self.kept_cache.add((pod, rel))
                    self.totals.kept_cache += 1
                    if written[rel] > 0:
                        emptiable.append((rel, written[rel]))
            finally:
                shutil.rmtree(root, ignore_errors=True)
        self.empty(pod, emptiable)
        return copied

    def _seed_tar(self) -> Optional[str]:
        """The seed directory as one tar file in staging, built once: only files in the cache layout."""
        if self.seed_tar is not None or not self.seed_dir:
            return self.seed_tar
        path = os.path.join(self.staging, SEED_TAR_NAME)
        count = 0
        try:
            with tarfile.open(path, "w") as tar:
                for dirpath, _, names in os.walk(self.seed_dir):
                    for name in sorted(names):
                        full = os.path.join(dirpath, name)
                        rel = os.path.relpath(full, self.seed_dir).replace(os.sep, "/")
                        if CACHE_REL_RE.match(rel) and os.path.isfile(full) and not os.path.islink(full):
                            tar.add(full, arcname=rel, recursive=False)
                            count += 1
        except OSError:
            self.fail("seed_tar")
            return None
        self.seed_tar = path
        self.emit({"event": "seed_ready", "files": count})
        return path

    def seeded(self, pod: str) -> bool:
        proc = self.exec_in("seeded", pod, SEEDED_SCRIPT, [self.data_dir, self.cache_subdir, CACHE_SEED_MARKER])
        return proc is not None and proc.stdout.strip() == b"1"

    def seed(self, pod: str) -> bool:
        """Replay mode: write the seed into the pod's cache, then its marker. True when the pod took it."""
        tar_path = self._seed_tar()
        if tar_path is None:
            return False
        proc = self.exec_in("seed", pod, SEED_SCRIPT,
                            [self.data_dir, self.cache_subdir, CACHE_SEED_MARKER, CACHE_REL_RE.pattern],
                            stdin_path=tar_path)
        if proc is None:
            return False
        self.totals.seeded += 1
        return True

    def seed_all(self) -> Tuple[int, int]:
        """Seed every proxy pod not draining now; (those pods, pods that took it).

        A pod whose seed failed is listed again: one deleted while its copy ran
        (an eviction, a node drain) is gone, not a pod that refused the cache,
        so it leaves both counts. One still live counts as a failure.
        """
        listed = self.pods()
        live = [pod.name for pod in listed if not pod.draining]
        if len(live) < len(listed):
            self.emit({"event": "seed_skipped_draining", "pods": len(listed) - len(live)})
        failed = [pod for pod in live if not self.seed(pod)]
        if not failed:
            return len(live), len(live)
        still_live = {pod.name for pod in self.pods() if not pod.draining}
        vanished = [pod for pod in failed if pod not in still_live]
        if vanished:
            self.emit({"event": "seed_skipped_vanished", "pods": len(vanished)})
        return len(live) - len(vanished), len(live) - len(failed)

    def _publish(self, final: bool) -> None:
        try:
            self.publisher.publish(final=final)  # type: ignore[union-attr]
        except Exception as exc:  # a failed publish is retried by the next one
            self.fail("publish_crashed", reason=type(exc).__name__)

    def maybe_publish(self, final: bool) -> None:
        """Record mode: publish every publish_interval seconds, beside collection, and always in the last pass."""
        if self.publisher is None:
            return
        running = self.publish_thread is not None and self.publish_thread.is_alive()
        if final:
            if running:
                self.publish_thread.join(max(0.0, self.deadline - self.clock()) if self.deadline else None)  # type: ignore[union-attr]
                if self.publish_thread.is_alive():  # type: ignore[union-attr]
                    self.fail("publish_overran")
                    return
            self.last_publish = self.clock()
            self._publish(final=True)
            return
        now = self.clock()
        if running or (self.last_publish is not None and now - self.last_publish < self.publish_interval):
            return
        self.last_publish = now
        if not self.publish_in_background:
            self._publish(final=False)
            return
        self.publish_thread = threading.Thread(target=self._publish, args=(False,), name=PUBLISH_THREAD_NAME,
                                               daemon=True)
        self.publish_thread.start()

    def write_lane_report(self) -> None:
        """Replay mode's last step: the lane's report, into the job's prefix under report/."""
        if self.lane_report is None:
            return
        root = tempfile.mkdtemp(dir=self.staging)
        try:
            try:
                summary = self.lane_report(root)
            except Exception as exc:  # the report is never the eval's problem
                self.fail("lane_report_crashed", reason=type(exc).__name__)
                return
            names = sorted(n for n in os.listdir(root) if os.path.isfile(os.path.join(root, n)))
            if names:
                paths = "\n".join(os.path.join(root, n) for n in names).encode() + NEWLINE
                cmd = ["gcloud", "storage", "cp", "--no-clobber", "-I", f"{self.dest}/{REPORT_DIR}/"]
                uploaded = self.run("report", cmd, GCLOUD_TIMEOUT_SECONDS, input=paths, env=GCLOUD_ENV) is not None
                self.emit({"event": "lane_report", "uploaded": uploaded, "files": len(names)})
            if summary:
                for line in summary.splitlines():
                    self.emit({"event": "lane_summary", "line": line})
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def write_manifest(self, job: Dict[str, str], started: float) -> bool:
        """Upload `job-<start ms>.json`, which says this build ran with the proxy even if it logged nothing."""
        root = tempfile.mkdtemp(dir=self.staging)
        try:
            name = f"{JOB_MANIFEST_PREFIX}{int(started * MS_PER_SECOND)}{JSON_SUFFIX}"
            path = os.path.join(root, name)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(dict(job, started=started), handle, sort_keys=True)
            cmd = ["gcloud", "storage", "cp", "--no-clobber", path, f"{self.dest}/{name}"]
            return self.run("manifest", cmd, GCLOUD_TIMEOUT_SECONDS, env=GCLOUD_ENV) is not None
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def collect_pass(self, final: bool = False) -> Dict[str, int]:
        """One pass over every proxy pod; `final` also takes the open segments."""
        stats = {"pods": 0, "listed": 0, "new": 0, "uploaded": 0}
        budget = PASS_MAX_BYTES
        for pod, draining in self.pods():
            if self.cut_short(final):
                break
            stats["pods"] += 1
            if self.mode == MODE_REPLAY and self.seed_dir and not draining and not self.seeded(pod):
                self.seed(pod)
            files = self.list_files(pod, with_open=final)
            stats["listed"] += len(files)
            new = [(rel, size) for rel, size in files if object_name(rel) not in self.uploaded]
            stats["new"] += len(new)
            # Uploaded already, from this pod or another: a blob both pods
            # wrote, or a file whose emptying failed last time.
            emptiable = [(rel, size) for rel, size in files if object_name(rel) in self.uploaded]
            for batch in batches(new, BATCH_MAX_BYTES):
                if budget <= 0 or self.cut_short(final):
                    break
                budget -= sum(size for _, size in batch)
                root = tempfile.mkdtemp(dir=self.staging)
                try:
                    written = self.fetch(pod, batch, root)
                    self.keep(pod, root, written)
                    uploaded = set(self.upload(root, written))
                    for name in uploaded:
                        self.uploaded.add(name)
                        stats["uploaded"] += 1
                        self.totals.uploaded_files += 1
                    # The size the pod's file had is the size uploaded: a
                    # closed file never changes, and an open one is never emptied.
                    emptiable += [(rel, written[object_name(rel)]) for rel, _ in batch
                                  if object_name(rel) in uploaded]
                finally:
                    shutil.rmtree(root, ignore_errors=True)
            self.empty(pod, emptiable)
            if self.mode == MODE_RECORD and self.keep_dir and budget > 0 and not self.cut_short(final):
                budget -= self.collect_cache(pod, budget, final)
        self.totals.passes += 1
        self.maybe_publish(final)
        return stats

    def report(self, stats: Dict[str, int], final: bool) -> None:
        self.emit({
            "event": "pass", "final": final, **stats,
            "total_uploaded_files": self.totals.uploaded_files,
            "total_uploaded_bytes": self.totals.uploaded_bytes,
            "total_emptied": self.totals.emptied,
            "total_kept_cache": self.totals.kept_cache,
            "total_seeded": self.totals.seeded,
            "errors": self.totals.errors,
        })


def parent_alive(pid: int) -> bool:
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run_loop(collector: Collector, interval: float, final_deadline: float, parent_pid: int,
             stop: Callable[[], bool], sleep: Callable[[float], None] = time.sleep,
             manifest: Optional[Callable[[], bool]] = None) -> None:
    """Pass every `interval` until `stop()` or the parent is gone, then one bounded last pass.

    `manifest` uploads the job manifest; it is tried before each pass until it
    succeeds. A pass that raises is counted and the loop goes on.
    """
    manifest_done = manifest is None

    def one_pass(final: bool) -> None:
        nonlocal manifest_done
        try:
            if not manifest_done:
                manifest_done = bool(manifest())  # type: ignore[misc]
            collector.report(collector.collect_pass(final=final), final=final)
            if final:
                collector.write_lane_report()
        except Exception as exc:  # one bad pass is not the end of collection
            collector.fail("pass_crashed", reason=type(exc).__name__)

    while not stop() and parent_alive(parent_pid):
        one_pass(final=False)
        waited = 0.0
        while waited < interval and not stop() and parent_alive(parent_pid):
            sleep(SLEEP_SLICE_SECONDS)
            waited += SLEEP_SLICE_SECONDS
    if collector.deadline is None:  # a SIGTERM has set it already
        collector.deadline = collector.clock() + final_deadline
    one_pass(final=True)


def _shaped(pattern: "re.Pattern[str]", what: str) -> Callable[[str], str]:
    def check(value: str) -> str:
        if not pattern.match(value):
            raise argparse.ArgumentTypeError(f"not a {what}")
        return value
    return check


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--context", default="")
    parser.add_argument("--selector", required=True, help="label selector of the proxy pods")
    parser.add_argument("--container", required=True, help="the proxy container's name")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="the proxy's data volume, in the pod")
    parser.add_argument("--bucket", required=True, type=_shaped(BUCKET_SHAPE, "bucket name"))
    parser.add_argument("--kind", required=True, choices=JOB_KINDS)
    parser.add_argument("--build", required=True, type=_shaped(BUILD_SHAPE, "build id"))
    parser.add_argument("--project", default="")
    parser.add_argument("--pr", default="")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--final-deadline", type=float, default=DEFAULT_FINAL_DEADLINE_SECONDS)
    parser.add_argument("--parent-pid", type=int, default=0, help="stop when this process is gone")
    parser.add_argument("--mode", choices=MODES, default=MODE_SHADOW, help="the proxy's REPLAY_MODE")
    parser.add_argument("--keep-dir", default="", help="record and replay: keep lines, stored text and answers here")
    parser.add_argument("--cache-subdir", default=DEFAULT_CACHE_SUBDIR, help="the cache directory, in the data volume")
    parser.add_argument("--verdicts-dir", default="", help="where bench-gate writes case-<name>.json")
    parser.add_argument("--publish-bucket", default="", help="record: the shared replay cache bucket")
    parser.add_argument("--publish-interval", type=float, default=DEFAULT_PUBLISH_INTERVAL_SECONDS)
    parser.add_argument("--job-name", default="", help="record: the Prow job name, written into each record")
    parser.add_argument("--eval-mode-next", default="", help="record and replay: EVAL_MODE_NEXT (1 is next mode)")
    parser.add_argument("--forge", default="", help="record and replay: EVAL_FORGE (empty is github)")
    parser.add_argument("--seed-dir", default="", help="replay: the cache fetch_cache.py filled, seeded into each pod")
    parser.add_argument("--seed-once", action="store_true", help="replay: seed every live proxy pod, then exit")
    parser.add_argument("--nightly-runs", default="", help="replay: fetch_cache.py's runs per recording build")
    args = parser.parse_args(argv)
    if not CACHE_SUBDIR_RE.match(args.cache_subdir) or args.cache_subdir in (LOGS_DIR, STORE_DIR):
        parser.error("--cache-subdir must be one plain directory name other than logs and store")
    if args.mode != MODE_SHADOW and not args.keep_dir:
        parser.error(f"--mode {args.mode} needs --keep-dir")
    if args.mode == MODE_RECORD and not (args.publish_bucket and args.verdicts_dir):
        parser.error("--mode record needs --publish-bucket and --verdicts-dir")
    if args.mode == MODE_REPLAY and not args.seed_dir:
        parser.error("--mode replay needs --seed-dir")
    if args.seed_once and args.mode != MODE_REPLAY:
        parser.error("--seed-once is for --mode replay")
    return args


def make_publisher(args: argparse.Namespace, collector: "Collector"):
    """Record mode's publisher (publish_cache.py), uploading through the collector's bounded commands."""
    if args.mode != MODE_RECORD:
        return None
    import publish_cache  # only record mode needs it, and its imports

    if not publish_cache.valid_bucket_and_build(args.publish_bucket, args.build):
        collector.fail("publish_bad_arguments")
        return None
    return publish_cache.Publisher(keep_dir=args.keep_dir, verdicts_dir=args.verdicts_dir,
                                   bucket=args.publish_bucket, build=args.build, run=collector.run,
                                   env=GCLOUD_ENV, emit=collector.emit,
                                   job=publish_cache.job_identity(args.job_name, args.eval_mode_next, args.forge))


def make_lane_report(args: argparse.Namespace) -> Optional[Callable[[str], Optional[str]]]:
    """Replay mode's last step (lane_report.py), when the verdicts are known to it."""
    if args.mode != MODE_REPLAY or not args.verdicts_dir:
        return None

    def report(out_dir: str) -> Optional[str]:
        import lane_report

        return lane_report.write_report(args.keep_dir, args.verdicts_dir, args.nightly_runs, out_dir)

    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    # First, so a TERM that arrives while the collector is still starting asks
    # for the last pass instead of killing it.
    stopping = {"flag": False}
    collector: Optional[Collector] = None
    final_deadline = DEFAULT_FINAL_DEADLINE_SECONDS

    def start_last_pass() -> None:
        # The last pass's clock starts now, and cuts whatever is running.
        if collector is not None and collector.deadline is None:
            collector.deadline = collector.clock() + final_deadline

    def on_term(_signum, _frame):
        stopping["flag"] = True
        start_last_pass()

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    # --seed-once answers a question (did every pod take the cache?), so it
    # exits 1 on anything but yes; everything else never fails the eval.
    seed_once = "--seed-once" in (sys.argv[1:] if argv is None else argv)
    status = 1 if seed_once else 0
    try:
        args = parse_args(argv)
    except SystemExit:  # argparse has said what was wrong; still never the eval's problem
        return status
    final_deadline = args.final_deadline
    started = time.time()
    day = time.strftime(DATE_FORMAT, time.gmtime(started))
    staging = ""
    try:
        staging = tempfile.mkdtemp(prefix=STAGING_PREFIX)
        collector = Collector(namespace=args.namespace, selector=args.selector, container=args.container,
                              dest=job_prefix(args.bucket, args.kind, day, args.build), staging=staging,
                              context=args.context, data_dir=args.data_dir, mode=args.mode,
                              keep_dir=args.keep_dir, cache_subdir=args.cache_subdir, seed_dir=args.seed_dir,
                              publish_interval=args.publish_interval, lane_report=make_lane_report(args))
        collector.publisher = make_publisher(args, collector)
        if args.seed_once:
            pods, took = collector.seed_all()
            collector.emit({"event": "seed_once", "pods": pods, "seeded": took, "errors": collector.totals.errors})
            status = 0 if pods and took == pods else 1
            return status
        if stopping["flag"]:
            start_last_pass()
        collector.emit({"event": "start", "kind": args.kind, "day": day, "interval": args.interval})
        job = {"kind": args.kind, "project": args.project, "build": args.build, "pr": args.pr}
        run_loop(collector, args.interval, args.final_deadline, args.parent_pid, lambda: stopping["flag"],
                 manifest=lambda: collector.write_manifest(job, started))  # type: ignore[union-attr]
    except Exception as exc:  # never the eval's problem
        if collector is not None:
            collector.emit({"event": "crashed", "reason": type(exc).__name__})
    finally:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)
        if collector is not None:
            collector.emit({"event": "stop", "uploaded_files": collector.totals.uploaded_files,
                            "uploaded_bytes": collector.totals.uploaded_bytes,
                            "emptied": collector.totals.emptied, "errors": collector.totals.errors})
    return status


if __name__ == "__main__":
    sys.exit(main())
