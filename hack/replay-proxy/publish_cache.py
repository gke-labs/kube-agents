"""Upload a trusted nightly's recorded answers, from runs that passed, to the shared replay cache.

A nightly whose proxy runs in record mode saves answers in the proxy pods
(replay_cache.py). collect_shadow.py copies them out of the cluster while the
eval runs, with the lines and the stored request text, into a directory on the
job's own machine (`--keep-dir`), and calls this every few minutes and once at
the end. Each call works out which run each recorded answer came from
(run_attribution.py) against the cases graded so far (bench-gate's
case-<name>.json under the artifacts directory), and uploads the answers whose
runs all passed (examples/inference-replay/eval-design.md, "Only save answers
from runs that passed").

What goes up, under gs://<bucket>/replay-cache/<normalizer version>/:
- `answers/<fingerprint>.json`: an answer, only when the request that saved it
  belongs to runs that passed. The key already holds the normalizer version,
  the LiteLLM config checksum and image, so answers no other run could hit
  cost space only; the version prefix lets a reader fetch only its own.
- `slots/<slot key>.json`: a slot pointer, only to an answer that went up.
- `variants/<context>-<answer>.json`: every answer a recording gave at a point,
  flat (replay_cache.py nests them one directory per context). A variant only
  vouches that a model really wrote an earlier answer in a history; what is
  served always comes from an answer above.
- `messages/<hash>.json`: the normalized, scrubbed messages a replay miss's
  snippets are drawn from.
And gs://<bucket>/replay-cache/builds/<build>/publish-<ms>.json, one per call
that changed anything: the graded runs and their outcomes, what was decided,
and the names this call confirmed are in the bucket (uploaded now, or there
already from an earlier night). The replay lane reads these to fetch exactly
the answers recent nightlies selected (fetch_cache.py) and to compare verdicts
(lane_report.py).

Every object is written once (`gcloud storage cp --no-clobber`, and the
recorder's grant cannot overwrite), so the first night to record a key keeps
it. An answer whose runs are not all graded yet waits for a later call; the
last call refuses what is still undecided.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import replay_cache
import run_attribution as RA
import schema as F

CACHE_ROOT = "replay-cache"
BUILDS_DIR = "builds"
PUBLISH_RECORD_PREFIX = "publish-"
PODS_DIR = "pods"
POD_LOGS_DIR = "logs"
POD_CACHE_DIR = "cache"
ANSWERS = replay_cache.ANSWERS_DIR
SLOTS = replay_cache.SLOTS_DIR
VARIANTS = replay_cache.VARIANTS_DIR
MESSAGES = replay_cache.MESSAGES_DIR
# Upload order: an answer before the slot pointing at it.
UPLOAD_ORDER = (ANSWERS, VARIANTS, SLOTS, MESSAGES)
JSON_SUFFIX = ".json"
SEGMENT_SUFFIX = ".jsonl"
VARIANT_SEPARATOR = "-"
HEX = r"[0-9a-f]{16,64}"
SAFE_HEX = re.compile(rf"^{HEX}$")
NORMALIZER_VERSION_RE = re.compile(r"^[0-9a-f]{16}$")
POD_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")
BUILD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MS_PER_SECOND = 1000
GCLOUD_TIMEOUT_SECONDS = 300.0
TEXT_ENCODING = "utf-8"
UPLOAD_STAGING_PREFIX = "replay-cache-publish-"
RECORD_STAGING_PREFIX = "replay-cache-record-"
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,61}[a-z0-9]$")

# Why an answer was not uploaded, beside run_attribution's decisions.
NO_RECORDING_LINE = "no_recording_line"
VERSION_MISMATCH = "version_mismatch"
UNREADABLE = "unreadable"

Run = Callable[..., Optional[subprocess.CompletedProcess]]


def cache_prefix(bucket: str, version: str) -> str:
    return f"gs://{bucket}/{CACHE_ROOT}/{version}"


def object_name(sub: str, rel: str) -> Optional[str]:
    """The object name (under the version prefix) for a pod cache file `<sub>/<rel>`; None if it isn't one."""
    parts = rel.split("/")
    if sub == VARIANTS:
        if len(parts) == 2 and SAFE_HEX.match(parts[0]) and parts[1].endswith(JSON_SUFFIX) \
                and SAFE_HEX.match(parts[1][: -len(JSON_SUFFIX)]):
            return f"{VARIANTS}/{parts[0]}{VARIANT_SEPARATOR}{parts[1]}"
        return None
    if sub in (ANSWERS, SLOTS, MESSAGES) and len(parts) == 1 and parts[0].endswith(JSON_SUFFIX) \
            and SAFE_HEX.match(parts[0][: -len(JSON_SUFFIX)]):
        return f"{sub}/{parts[0]}"
    return None


def read_json(path: str):
    return RA.read_json(path)


def pod_dirs(keep_dir: str) -> List[Tuple[str, str]]:
    """(pod name, its directory) for every pod the collector kept files from."""
    root = os.path.join(keep_dir, PODS_DIR)
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    return [(n, os.path.join(root, n)) for n in names if POD_NAME_RE.match(n) and os.path.isdir(os.path.join(root, n))]


def segment_paths(pod_dir: str) -> List[str]:
    logs = os.path.join(pod_dir, POD_LOGS_DIR)
    try:
        return sorted(os.path.join(logs, n) for n in os.listdir(logs) if n.endswith(SEGMENT_SUFFIX))
    except OSError:
        return []


def pod_version(segments: Sequence[str]) -> str:
    """The normalizer version the pod's proxy started with (its start line), or empty."""
    for path in segments:
        try:
            with open(path, encoding=TEXT_ENCODING) as handle:
                for raw in handle:
                    try:
                        entry = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(entry, dict) and entry.get(F.EVENT) == F.START_EVENT:
                        version = str(entry.get(F.NORMALIZER_VERSION) or "")
                        if NORMALIZER_VERSION_RE.match(version):
                            return version
        except OSError:
            continue
    return ""


def pod_files(pod_dir: str, sub: str) -> List[Tuple[str, str]]:
    """(object name, local path) of every kept cache file under `sub` in one pod."""
    base = os.path.join(pod_dir, POD_CACHE_DIR, sub)
    out: List[Tuple[str, str]] = []
    for dirpath, _, names in os.walk(base):
        for name in names:
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                continue
            rel = os.path.relpath(path, base).replace(os.sep, "/")
            obj = object_name(sub, rel)
            if obj is not None:
                out.append((obj, path))
    return sorted(out)


@dataclass
class Selection:
    """What one call decided: objects to upload by version, and the counts behind them."""

    objects: Dict[str, Dict[str, Dict[str, str]]] = field(default_factory=dict)  # version -> sub -> name -> path
    slot_targets: Dict[str, Dict[str, str]] = field(default_factory=dict)  # version -> slot object -> fingerprint
    decisions: Dict[str, int] = field(default_factory=dict)
    runs: List[RA.Run] = field(default_factory=list)
    lost_lines: int = 0

    def add(self, version: str, sub: str, name: str, path: str) -> None:
        self.objects.setdefault(version, {}).setdefault(sub, {}).setdefault(name, path)

    def count(self, decision: str) -> None:
        self.decisions[decision] = self.decisions.get(decision, 0) + 1


def select(keep_dir: str, verdicts_dir: str, final: bool, store: Optional[RA.Store] = None) -> Selection:
    """Decide which kept cache files may go up, against the cases graded so far.

    `store`, kept between calls, reads each stored message once per process.
    """
    selection = Selection(runs=RA.load_runs(verdicts_dir))
    pods = [(name, path, segment_paths(path)) for name, path in pod_dirs(keep_dir)]
    logs = RA.load_logs(p for _, _, segments in pods for p in segments)
    selection.lost_lines = logs.lost
    attribution = RA.Attribution(logs, store or RA.Store(keep_dir), selection.runs, final)
    for _, pod_dir, segments in pods:
        version = pod_version(segments)
        if not version:
            continue
        recorded: Dict[str, RA.Line] = {}
        for line in RA.load_logs(segments).lines:
            if line.recorded:
                recorded.setdefault(line.fingerprint, line)
        for name, path in pod_files(pod_dir, ANSWERS):
            fingerprint = name[len(ANSWERS) + 1: -len(JSON_SUFFIX)]
            line = recorded.get(fingerprint)
            if line is None:
                selection.count(NO_RECORDING_LINE)
                continue
            entry = read_json(path)
            if not isinstance(entry, dict) or entry.get(replay_cache.ENTRY_FINGERPRINT) != fingerprint:
                selection.count(UNREADABLE)
                continue
            if entry.get(replay_cache.ENTRY_NORMALIZER_VERSION) != version:
                selection.count(VERSION_MISMATCH)
                continue
            decision = attribution.decide(line).decision
            selection.count(decision)
            if decision == RA.PASS:
                selection.add(version, ANSWERS, name, path)
        for sub in (VARIANTS, MESSAGES):
            for name, path in pod_files(pod_dir, sub):
                selection.add(version, sub, name, path)
        for name, path in pod_files(pod_dir, SLOTS):
            target = read_json(path)
            if isinstance(target, str) and SAFE_HEX.match(target):
                selection.add(version, SLOTS, name, path)
                selection.slot_targets.setdefault(version, {}).setdefault(name, target)
    return selection


class Publisher:
    """Uploads what select() allows and has not gone up yet; called repeatedly, then once with final=True."""

    def __init__(self, *, keep_dir: str, verdicts_dir: str, bucket: str, build: str, run: Run,
                 env: Optional[Dict[str, str]] = None, emit: Callable[[Dict[str, object]], None] = lambda r: None,
                 clock: Callable[[], float] = time.time):
        self.keep_dir = keep_dir
        self.verdicts_dir = verdicts_dir
        self.bucket = bucket
        self.build = build
        self.run = run
        self.env = env
        self.emit = emit
        self.clock = clock
        self.uploaded: Set[Tuple[str, str]] = set()  # (version, object name)
        self.recorded_runs: Optional[Tuple[Tuple[str, str], ...]] = None
        # Names confirmed in the bucket whose record did not upload yet.
        self.unrecorded: Dict[str, Set[str]] = {}
        self.store = RA.Store(keep_dir)
        self.totals: Dict[str, int] = {}

    def _upload(self, version: str, sub: str, items: Dict[str, str]) -> List[str]:
        """Upload `items` (object name -> local path) into one directory; returns the names that went up."""
        stage = tempfile.mkdtemp(prefix=UPLOAD_STAGING_PREFIX)
        try:
            paths = []
            for name, path in sorted(items.items()):
                staged = os.path.join(stage, os.path.basename(name))
                shutil.copyfile(path, staged)
                paths.append(staged)
            cmd = ["gcloud", "storage", "cp", "--no-clobber", "-I", f"{cache_prefix(self.bucket, version)}/{sub}/"]
            data = ("\n".join(paths) + "\n").encode()
            if self.run("publish", cmd, GCLOUD_TIMEOUT_SECONDS, input=data, env=self.env) is None:
                return []
            return sorted(items)
        except OSError:
            return []
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def _record(self, selection: Selection, final: bool, confirmed: Dict[str, List[str]]) -> None:
        """Upload this call's record: the graded runs, the decisions and the names confirmed in the bucket."""
        runs = tuple(sorted((run.key, run.outcome) for run in selection.runs))
        if not final and not confirmed and (not runs or runs == self.recorded_runs):
            return
        stage = tempfile.mkdtemp(prefix=RECORD_STAGING_PREFIX)
        try:
            name = f"{PUBLISH_RECORD_PREFIX}{int(self.clock() * MS_PER_SECOND)}{JSON_SUFFIX}"
            path = os.path.join(stage, name)
            with open(path, "w", encoding=TEXT_ENCODING) as handle:
                json.dump({
                    "build": self.build, "final": final,
                    "runs": [{"case": r.case, "rep": r.rep, "outcome": r.outcome} for r in selection.runs],
                    "decisions": selection.decisions, "lost_lines": selection.lost_lines,
                    "objects": {version: sorted(names) for version, names in confirmed.items()},
                }, handle, sort_keys=True)
            dest = f"gs://{self.bucket}/{CACHE_ROOT}/{BUILDS_DIR}/{self.build}/{name}"
            if self.run("publish_record", ["gcloud", "storage", "cp", "--no-clobber", path, dest],
                        GCLOUD_TIMEOUT_SECONDS, env=self.env) is not None:
                self.recorded_runs = runs
            else:
                # Retried on the next call, which records these names again.
                for version, names in confirmed.items():
                    self.unrecorded.setdefault(version, set()).update(names)
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def publish(self, final: bool = False) -> Dict[str, object]:
        selection = select(self.keep_dir, self.verdicts_dir, final, self.store)
        uploaded_now: Dict[str, int] = {}
        confirmed: Dict[str, List[str]] = {v: sorted(n) for v, n in self.unrecorded.items()}
        self.unrecorded = {}
        for version, subs in sorted(selection.objects.items()):
            for sub in UPLOAD_ORDER:
                items = {n: p for n, p in subs.get(sub, {}).items() if (version, n) not in self.uploaded}
                if sub == SLOTS:
                    # A pointer goes up only once the answer it names has.
                    targets = selection.slot_targets.get(version, {})
                    items = {n: p for n, p in items.items()
                             if (version, f"{ANSWERS}/{targets.get(n, '')}{JSON_SUFFIX}") in self.uploaded}
                if not items:
                    continue
                done = self._upload(version, sub, items)
                for name in done:
                    self.uploaded.add((version, name))
                confirmed.setdefault(version, []).extend(done)
                uploaded_now[sub] = uploaded_now.get(sub, 0) + len(done)
                self.totals[sub] = self.totals.get(sub, 0) + len(done)
        self._record(selection, final, confirmed)
        report: Dict[str, object] = {
            "event": "publish", "final": final, "runs": len(selection.runs),
            "decisions": selection.decisions, "uploaded": uploaded_now, "total_uploaded": dict(self.totals),
            "lost_lines": selection.lost_lines,
        }
        self.emit(report)
        return report


def valid_bucket(bucket: str) -> bool:
    return bool(BUCKET_RE.match(bucket))


def valid_bucket_and_build(bucket: str, build: str) -> bool:
    return valid_bucket(bucket) and bool(BUILD_RE.match(build)) and ".." not in build
