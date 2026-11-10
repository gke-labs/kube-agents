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
and the objects this call confirmed are in the bucket (uploaded now, or there
already from an earlier night), each with its size in this night's copy and,
for a slot, the fingerprint of the answer it points at. The replay lane reads these to fetch exactly
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
JSON_SUFFIX = replay_cache.JSON_SUFFIX
VARIANT_SEPARATOR = "-"
SEGMENT_SUFFIX = ".jsonl"
HEX = replay_cache.CACHE_HEX
SAFE_HEX = re.compile(rf"^{HEX}$")
# A cache object's name under the version prefix: replay_cache.CACHE_REL_RE's
# layout with each variant flattened to one name, `variants/<context>-<answer>.json`.
OBJECT_NAME_RE = re.compile(replay_cache.CACHE_LAYOUT_PATTERN.replace(replay_cache.VARIANT_JOINER, re.escape(VARIANT_SEPARATOR)))
NORMALIZER_VERSION_RE = re.compile(r"^[0-9a-f]{16}$")
POD_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")
# A build id as a path segment: hack/ci-env.sh's REPLAY_SHADOW_BUILD_RE, which
# tests/test_ci_replay_shadow.py holds equal to this.
BUILD_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]*"
BUILD_RE = re.compile(rf"^{BUILD_PATTERN}$")
# A publish record's fields, read by fetch_cache.py and lane_report.py.
RECORD_BUILD = "build"
RECORD_FINAL = "final"
RECORD_RUNS = "runs"
RECORD_DECISIONS = "decisions"
RECORD_LOST_LINES = "lost_lines"
RECORD_OBJECTS = "objects"
# The recording job: its Prow job name, the install mode it graded (next or
# today) and its forge (github when EVAL_FORGE is unset). The lane fetches
# objects only from records whose mode and forge match its own run's, so a
# GitLab or today-mode night's answers, which it can't hit, never take its room.
RECORD_JOB = "job"
JOB_NAME = "name"
JOB_MODE = "mode"
JOB_FORGE = "forge"
MODE_NEXT = "next"
MODE_TODAY = "today"
DEFAULT_FORGE = "github"
RUN_CASE = "case"
RUN_REP = "rep"
RUN_OUTCOME = "outcome"
# Each object a record confirms: its name, its size in the recorder's copy, and
# for a slot the fingerprint of the answer it points at.
OBJECT_NAME = "name"
OBJECT_BYTES = "bytes"
OBJECT_TARGET = "target"
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
    if not replay_cache.CACHE_REL_RE.match(f"{sub}/{rel}"):
        return None
    if sub == VARIANTS:
        context, answer = rel.split("/")
        return f"{VARIANTS}/{context}{VARIANT_SEPARATOR}{answer}"
    return f"{sub}/{rel}"


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


def pod_version(segments: Sequence[str], known: Optional[Dict[str, str]] = None) -> str:
    """The normalizer version the pod's proxy started with (its start line), or empty.

    `known`, kept between calls, maps a segment already read to the version it gave.
    """
    for path in segments:
        if known is not None and path in known:
            return known[path]
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
                            if known is not None:
                                known[path] = version
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


@dataclass
class State:
    """What a Publisher keeps between calls, so each call reads only what is new."""

    store: Optional[RA.Store] = None
    reader: RA.LogReader = field(default_factory=RA.LogReader)
    results: Dict[Tuple[str, int, int], Set[str]] = field(default_factory=dict)
    versions: Dict[str, str] = field(default_factory=dict)
    # (version, object name) already in the bucket: not read or decided again.
    done: Set[Tuple[str, str]] = field(default_factory=set)


def select(keep_dir: str, verdicts_dir: str, final: bool, state: Optional[State] = None) -> Selection:
    """Decide which kept cache files may go up, against the cases graded so far."""
    state = state or State()
    if state.store is None:
        state.store = RA.Store(keep_dir)
    selection = Selection(runs=RA.load_runs(verdicts_dir, state.results))
    pods = [(name, path, segment_paths(path)) for name, path in pod_dirs(keep_dir)]
    logs = state.reader.load(p for _, _, segments in pods for p in segments)
    selection.lost_lines = logs.lost
    attribution = RA.Attribution(logs, state.store, selection.runs, final)
    for _, pod_dir, segments in pods:
        version = pod_version(segments, state.versions)
        if not version:
            continue
        recorded: Dict[str, RA.Line] = {}
        for line in state.reader.load(segments).lines:
            if line.recorded:
                recorded.setdefault(line.fingerprint, line)
        for name, path in pod_files(pod_dir, ANSWERS):
            if (version, name) in state.done:
                continue
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
            decision = attribution.decide_answer(line).decision
            selection.count(decision)
            if decision == RA.PASS:
                selection.add(version, ANSWERS, name, path)
        for sub in (VARIANTS, MESSAGES):
            for name, path in pod_files(pod_dir, sub):
                if (version, name) not in state.done:
                    selection.add(version, sub, name, path)
        for name, path in pod_files(pod_dir, SLOTS):
            if (version, name) in state.done:
                continue
            target = read_json(path)
            if isinstance(target, str) and SAFE_HEX.match(target):
                selection.add(version, SLOTS, name, path)
                selection.slot_targets.setdefault(version, {}).setdefault(name, target)
    return selection


class Publisher:
    """Uploads what select() allows and has not gone up yet; called repeatedly, then once with final=True."""

    def __init__(self, *, keep_dir: str, verdicts_dir: str, bucket: str, build: str, run: Run,
                 env: Optional[Dict[str, str]] = None, emit: Callable[[Dict[str, object]], None] = lambda r: None,
                 clock: Callable[[], float] = time.time, job: Optional[Dict[str, str]] = None):
        self.keep_dir = keep_dir
        # The recording job's identity (RECORD_JOB), written into every record.
        self.job = dict(job or {})
        self.verdicts_dir = verdicts_dir
        self.bucket = bucket
        self.build = build
        self.run = run
        self.env = env
        self.emit = emit
        self.clock = clock
        self.state = State(store=RA.Store(keep_dir))
        self.uploaded = self.state.done  # (version, object name) in the bucket
        self.recorded_runs: Optional[Tuple[Tuple[str, str], ...]] = None
        # Names confirmed in the bucket whose record did not upload yet.
        self.unrecorded: Dict[str, Dict[str, Dict[str, object]]] = {}
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

    def _record(self, selection: Selection, final: bool, confirmed: Dict[str, Dict[str, Dict[str, object]]]) -> None:
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
                    RECORD_BUILD: self.build, RECORD_FINAL: final, RECORD_JOB: self.job,
                    RECORD_RUNS: [{RUN_CASE: r.case, RUN_REP: r.rep, RUN_OUTCOME: r.outcome} for r in selection.runs],
                    RECORD_DECISIONS: selection.decisions, RECORD_LOST_LINES: selection.lost_lines,
                    RECORD_OBJECTS: {version: [objects[n] for n in sorted(objects)]
                                     for version, objects in confirmed.items()},
                }, handle, sort_keys=True)
            dest = f"gs://{self.bucket}/{CACHE_ROOT}/{BUILDS_DIR}/{self.build}/{name}"
            if self.run("publish_record", ["gcloud", "storage", "cp", "--no-clobber", path, dest],
                        GCLOUD_TIMEOUT_SECONDS, env=self.env) is not None:
                self.recorded_runs = runs
            else:
                # Retried on the next call, which records these names again.
                for version, objects in confirmed.items():
                    self.unrecorded.setdefault(version, {}).update(objects)
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def publish(self, final: bool = False) -> Dict[str, object]:
        selection = select(self.keep_dir, self.verdicts_dir, final, self.state)
        uploaded_now: Dict[str, int] = {}
        confirmed: Dict[str, Dict[str, Dict[str, object]]] = {v: dict(o) for v, o in self.unrecorded.items()}
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
                targets = selection.slot_targets.get(version, {})
                for name in done:
                    self.uploaded.add((version, name))
                    described: Dict[str, object] = {OBJECT_NAME: name, OBJECT_BYTES: _size(items[name])}
                    if sub == SLOTS:
                        described[OBJECT_TARGET] = targets.get(name, "")
                    confirmed.setdefault(version, {})[name] = described
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


def job_identity(name: str, mode_next: str, forge: str) -> Dict[str, str]:
    """The record's job block from the eval's own settings (EVAL_MODE_NEXT, EVAL_FORGE)."""
    return {JOB_NAME: name, JOB_MODE: MODE_NEXT if mode_next == "1" else MODE_TODAY,
            JOB_FORGE: forge or DEFAULT_FORGE}


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def valid_bucket(bucket: str) -> bool:
    return bool(BUCKET_RE.match(bucket))


def valid_bucket_and_build(bucket: str, build: str) -> bool:
    return valid_bucket(bucket) and bool(BUILD_RE.match(build)) and ".." not in build
