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
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

BUCKET_ROOT = "replay-shadow"
JOB_KINDS = ("nightly", "presubmit")
DATE_FORMAT = "%Y-%m-%d"
JOB_MANIFEST_PREFIX = "job-"
JSON_SUFFIX = ".json"
SEGMENT_SUFFIX = ".jsonl"
OPEN_SUFFIX = ".open"
LOGS_DIR = "logs"
STORE_REQUESTS_DIR = "store/requests"
STORE_BLOBS_DIR = "store/blobs"
# What a pod's data volume may hold that is ours to copy, by directory, and the
# suffix a complete file there has.
SOURCES = ((LOGS_DIR, SEGMENT_SUFFIX), (STORE_REQUESTS_DIR, JSON_SUFFIX), (STORE_BLOBS_DIR, JSON_SUFFIX))

# The chart closes a segment 30 s after its first line
# (REPLAY_LOG_SEGMENT_SECONDS), so at this interval a line is in the bucket
# about a minute after the proxy wrote it.
DEFAULT_INTERVAL_SECONDS = 30.0
DEFAULT_FINAL_DEADLINE_SECONDS = 60.0
DEFAULT_DATA_DIR = "/data"
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
    if not rel or rel.endswith(".open") or rel.endswith(".tmp"):
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
"""


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
                   env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    """Run `cmd`, killing it once `remaining()` reaches 0.

    `remaining` is asked again every SLEEP_SLICE_SECONDS rather than once, so a
    SIGTERM that brings the collector's deadline forward also cuts a command
    already running.
    """
    merged = dict(os.environ)
    merged.update(env or {})
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


@dataclass
class Totals:
    passes: int = 0
    uploaded_files: int = 0
    uploaded_bytes: int = 0
    emptied: int = 0
    errors: Dict[str, int] = field(default_factory=dict)

    def error(self, kind: str) -> int:
        """Count one error of `kind`; returns how many there have been."""
        self.errors[kind] = self.errors.get(kind, 0) + 1
        return self.errors[kind]


class Collector:
    def __init__(
        self, *, namespace: str, selector: str, container: str, dest: str, staging: str,
        context: str = "", data_dir: str = DEFAULT_DATA_DIR, runner: Runner = default_runner,
        out=None, clock: Callable[[], float] = time.monotonic,
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
            env: Optional[Dict[str, str]] = None) -> Optional[subprocess.CompletedProcess]:
        """Run one command; None (and an error counted and printed by class) when it fails."""
        started = self.clock()
        if self._remaining(started, default_timeout) <= 0:
            self.fail(f"{kind}_deadline")
            return None
        try:
            proc = self.runner(cmd, input=input, remaining=lambda: self._remaining(started, default_timeout), env=env)
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
                stdin: Optional[bytes] = None) -> Optional[subprocess.CompletedProcess]:
        cmd = self.kubectl + ["exec"] + (["-i"] if stdin is not None else []) + [
            pod, "-c", self.container, "--", "python3", "-c", script, *args]
        return self.run(kind, cmd, KUBECTL_TIMEOUT_SECONDS, input=stdin)

    # --- steps ------------------------------------------------------------

    def pods(self) -> List[str]:
        proc = self.run("list_pods", self.kubectl + ["get", "pods", "-l", self.selector, "-o", "json"],
                        KUBECTL_TIMEOUT_SECONDS)
        if proc is None:
            return []
        names = []
        try:
            for pod in json.loads(proc.stdout).get("items") or []:
                status = pod.get("status") or {}
                if status.get("phase") != POD_PHASE_RUNNING:
                    continue
                for cs in status.get("containerStatuses") or []:
                    if cs.get("name") == self.container and (cs.get("state") or {}).get("running") is not None:
                        names.append(str(pod["metadata"]["name"]))
        except (ValueError, AttributeError, KeyError, TypeError):
            self.fail("list_pods_parse")
            return []
        return sorted(set(names))

    def list_files(self, pod: str, with_open: bool) -> List[Tuple[str, int]]:
        proc = self.exec_in("list_files", pod, LIST_SCRIPT,
                            [self.data_dir, "1" if with_open else "0", json.dumps(SOURCES)])
        if proc is None:
            return []
        try:
            raw = json.loads(proc.stdout)
            return sorted((str(rel), int(size)) for rel, size in raw)
        except (ValueError, TypeError):
            self.fail("list_files_parse")
            return []

    def fetch(self, pod: str, batch: Sequence[Tuple[str, int]], root: str) -> Dict[str, int]:
        """Copy `batch` out of the pod into `root`; returns {object name: bytes written}.

        A member the stream cut short is not written: tarfile raises reading
        it, and only members read whole reach the disk.
        """
        wanted = {rel for rel, _ in batch}
        proc = self.exec_in("fetch", pod, TAR_SCRIPT, [self.data_dir], stdin=NUL.join(r.encode() for r in sorted(wanted)))
        if proc is None:
            return {}
        written: Dict[str, int] = {}
        try:
            with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r|") as tar:
                for member in tar:
                    if member.name not in wanted or not member.isfile():
                        continue
                    handle = tar.extractfile(member)
                    data = handle.read() if handle is not None else b""
                    if member.name.endswith(OPEN_SUFFIX):
                        data = whole_lines(data)
                    if not data:
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
        for pod in self.pods():
            if self.cut_short(final):
                break
            stats["pods"] += 1
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
        self.totals.passes += 1
        return stats

    def report(self, stats: Dict[str, int], final: bool) -> None:
        self.emit({
            "event": "pass", "final": final, **stats,
            "total_uploaded_files": self.totals.uploaded_files,
            "total_uploaded_bytes": self.totals.uploaded_bytes,
            "total_emptied": self.totals.emptied,
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


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--context", default="")
    parser.add_argument("--selector", required=True, help="label selector of the proxy pods")
    parser.add_argument("--container", required=True, help="the proxy container's name")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="the proxy's data volume, in the pod")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--kind", required=True, choices=JOB_KINDS)
    parser.add_argument("--build", required=True)
    parser.add_argument("--project", default="")
    parser.add_argument("--pr", default="")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--final-deadline", type=float, default=DEFAULT_FINAL_DEADLINE_SECONDS)
    parser.add_argument("--parent-pid", type=int, default=0, help="stop when this process is gone")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        args = parse_args(argv)
    except SystemExit:  # argparse has said what was wrong; still never the eval's problem
        return 0
    started = time.time()
    day = time.strftime(DATE_FORMAT, time.gmtime(started))
    stopping = {"flag": False}
    staging = ""
    collector: Optional[Collector] = None
    try:
        staging = tempfile.mkdtemp(prefix="replay-shadow-")
        collector = Collector(namespace=args.namespace, selector=args.selector, container=args.container,
                              dest=job_prefix(args.bucket, args.kind, day, args.build), staging=staging,
                              context=args.context, data_dir=args.data_dir)

        def on_term(_signum, _frame):
            # The last pass's clock starts now, and cuts whatever is running.
            stopping["flag"] = True
            if collector.deadline is None:  # type: ignore[union-attr]
                collector.deadline = collector.clock() + args.final_deadline  # type: ignore[union-attr]

        signal.signal(signal.SIGTERM, on_term)
        signal.signal(signal.SIGINT, on_term)
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
