#!/usr/bin/env python3
"""Measure how long CI runs wait before they get a pod, and say why when it is long.

The evaluation pool has a fixed number of projects. When demand outgrows it, the
symptom is not a failure -- every run still passes -- it is that runs sit in the
queue longer and longer, and nobody notices until someone happens to look at a
pull request that took three hours to start. GoogleCloudPlatform/oss-test-infra#2666
was found that way, by hand, after the fact; the runs that queued for an hour on
the other days of that fortnight were never looked at.

This measures the wait directly, from data Prow already writes, and prints:

  * a per-day table of p50/p95/max over the window, so a bad Tuesday does not
    disappear into a quiet week;
  * a roll-call of individual runs that waited too long, because a single
    75-minute wait among five runs moves no percentile and is exactly the case
    that goes unseen;
  * whether a breach is the pool being full (onboard another project) or the
    Prow control plane not dispatching (oss-test-infra#2666, where onboarding
    would spend money and fix nothing).

That last distinction is the one worth being careful about. A long wait with an
*idle* pool is not a capacity problem, and the only way to tell from outside is
to ask Boskos how many projects are actually leased at the time.

Three sources, three questions, and each degrades on its own rather than taking
the run down with it:

  GCS     pr-logs/, the trend over the window.        The onboarding gauge.
  Deck    prowjobs.js, what is queued right now.      The incident alarm.
  Boskos  /metric, how much of the pool is leased.    The capacity verdict.

Only GCS is required. Deck and Boskos each add a question this cannot answer
without them, and their absence is reported rather than assumed away.

Usage:
    python3 scripts/pool_pressure.py
    python3 scripts/pool_pressure.py --as-of 2026-08-26 --window-days 1
"""

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# The presubmit whose queue wait this measures. It is the only job in the pool
# that takes a Boskos lease, so it is the only one whose wait says anything
# about pool capacity.
JOB_NAME = "pull-kube-agents-smoke-test"

# Prow writes one tiny object per build under pr-logs/directory/<job>/, holding
# the full path of that build's artifact directory. Listing it is the only cheap
# way to enumerate builds: a recursive listing of pr-logs/pull/ takes seven
# minutes and a wildcard over the per-build subdirectories takes two, against
# under ten seconds to copy every object in here.
GCS_BUCKET = "kube-agents-prow"
GCS_BUILD_INDEX = f"gs://{GCS_BUCKET}/pr-logs/directory/{JOB_NAME}"
BUILD_INDEX_SUFFIX = ".txt"

# The three artifacts setup time is read from, in the order the run writes
# them. Only prowjob.json is required: initupload writes it when the pod starts,
# so it exists for every build that got one -- and for no build that did not,
# which is the blind spot the Deck source covers. A build missing either of the
# other two loses those segments, not the whole sample.
PROWJOB_ARTIFACT = "prowjob.json"
STARTED_ARTIFACT = "started.json"
BUILD_LOG_ARTIFACT = "build-log.txt"

# The two JSON artifacts are read in one call and come back concatenated, so
# each is recognised by its own shape rather than by the order they arrive in.
PROWJOB_KIND = "ProwJob"
STARTED_TIMESTAMP_KEY = "timestamp"

# How Boskos is reached. These are --boskos-via's choices and they are compared
# against in fetch_pool_state, so the argument parser and the dispatch read from
# one list rather than two spellings that can drift apart.
BOSKOS_VIA_KUBECTL = "kubectl"
BOSKOS_VIA_HTTP = "http"
BOSKOS_VIA_NONE = "none"
BOSKOS_VIA_CHOICES = (BOSKOS_VIA_KUBECTL, BOSKOS_VIA_HTTP, BOSKOS_VIA_NONE)
KUBECTL_BINARY = "kubectl"

# Unit conversions.
MILLIS_PER_SECOND = 1000.0
SECONDS_PER_HOUR = 3600
PERCENT_SCALE = 100.0

# --from-dir mirrors the bucket, one directory per artifact, named by build ID.
FIXTURE_PROWJOBS_DIR = "prowjobs"
FIXTURE_STARTED_DIR = "started"
FIXTURE_LOGS_DIR = "logs"
FIXTURE_LOG_SUFFIX = ".txt"

# How much of build-log.txt to read. The lease banner pair sat 6.8 kB into a
# 457 kB log on the worst build of oss-test-infra#2666, and `gcloud storage
# cat -r` transfers only the range, so the head costs a round trip rather than a
# download. Ten times that offset, for whatever the job prints before it leases.
BUILD_LOG_HEAD_BYTES = 65536

# Every phase boundary in the presubmit's script prints a banner:
#
#     === [2026-08-26T17:10:10Z] Leasing GCP Project from Boskos ===
#
# from prow/prowjobs/gke-labs/kube-agents/kube-agents-presubmits.yaml in
# GoogleCloudPlatform/oss-test-infra -- another repository, whose editors have no
# reason to know this parses it. So match the grammar and pick the lease banner
# out by keyword: a reworded line still parses while it still says Boskos. When
# it stops, SEGMENT_MIN_COVERAGE prints "not measured" rather than a median over
# whatever survived.
BANNER_PATTERN = re.compile(r"^===\s*\[([^]]+)\]\s*(.*?)\s*===\s*$", re.MULTILINE)
BANNER_LEASE_KEYWORD = "boskos"

# The four segments of setup time, in the order a run goes through them, with
# the label the report prints. Keys rather than prose because --json emits them.
SEGMENT_QUEUE = "queue"
SEGMENT_POD = "pod"
SEGMENT_SETUP = "setup"
SEGMENT_LEASE = "lease"
SEGMENT_LABELS = (
    (SEGMENT_QUEUE, "ProwJob queued  -> pod created"),
    (SEGMENT_POD, "pod created     -> container start"),
    (SEGMENT_SETUP, "container start -> lease requested"),
    (SEGMENT_LEASE, "Boskos acquire"),
)

# Below this share of the window's runs, a segment prints as unmeasured rather
# than a median. A median over three of ninety runs is a wrong answer with a
# number attached.
SEGMENT_MIN_COVERAGE = 0.5

# What a segment prints in place of a median, and the two column widths the
# breakdown is laid out on. The label column clears the longest label by a
# space on purpose: "container start -> lease requested" is exactly as wide as
# this string, so a fixed width equal to the label runs the two together.
SEGMENT_UNMEASURED = "not measured"
SEGMENT_LABEL_WIDTH = max(len(label) for _, label in SEGMENT_LABELS) + 1
SEGMENT_VALUE_WIDTH = len(SEGMENT_UNMEASURED)

# Prow build IDs are Twitter snowflakes: the top bits are milliseconds since a
# fixed epoch. That makes a build ID a timestamp, so the window can be narrowed
# before a single object is read -- the difference between reading 400 files and
# reading 1000. Empirically the encoded time tracks status.pendingTime rather
# than metadata.creationTimestamp, within a second, so it is a sound filter for
# the end of the wait and NOT for its start. SNOWFLAKE_SLACK covers both that
# jitter and a wait that straddles the window edge.
SNOWFLAKE_EPOCH_MS = 1288834974657
SNOWFLAKE_TIMESTAMP_SHIFT = 22
SNOWFLAKE_SLACK = timedelta(hours=6)

# Deck serves every tenant's prowjobs from one endpoint, unauthenticated. The
# omit list drops the pod spec and the label maps, which are most of the payload
# and none of what is read here.
DECK_PROWJOBS_URL = "https://oss.gprow.dev/prowjobs.js?omit=pod_spec,annotations,labels"

# prow/oss/config.yaml sets sinker.max_prowjob_age to 48h, so Deck cannot answer
# anything about a run older than that. It is the live view, not a history.
DECK_HORIZON = timedelta(hours=48)

# Boskos exposes pool occupancy on an unauthenticated endpoint, but only on a
# ClusterIP, so reaching it from outside the cluster means a port-forward.
BOSKOS_NAMESPACE = "boskos"
BOSKOS_SERVICE = "svc/boskos"
BOSKOS_SERVICE_PORT = 80
BOSKOS_RESOURCE_TYPE = "kube-agents-evals-project"
BOSKOS_METRIC_PATH = "/metric?type=" + BOSKOS_RESOURCE_TYPE
BOSKOS_IN_CLUSTER_URL = (
    f"http://boskos.{BOSKOS_NAMESPACE}.svc.cluster.local{BOSKOS_METRIC_PATH}"
)
PROW_BUILD_CLUSTER_CONTEXT = "gke_kube-agents-prow_us-west1-b_kube-agents-prow"

# The state a lease is in while a run holds it. Boskos omits states with a zero
# count from `current` rather than reporting them as 0, so a missing key means
# none, not unknown.
BOSKOS_STATE_BUSY = "busy"
BOSKOS_STATE_FREE = "free"
# Boskos files unleased resources under the empty-string owner. It is a count of
# nobody, not a real lease, and including it would make every idle pool look
# like it had one mystery holder.
BOSKOS_NO_OWNER = ""

# The policy in docs/site/src/content/docs/deploy/ci-pool-projects.md. A breach
# of either is the signal to onboard the next project -- if, and only if, the
# pool was actually full at the time.
DEFAULT_P50_THRESHOLD_MINUTES = 15
DEFAULT_P95_THRESHOLD_MINUTES = 45

# Reported one run at a time rather than as a percentile. A day with five runs,
# one of which waited 75 minutes, has a p95 that never leaves the noise -- the
# aggregate is the wrong instrument for a rare long wait, and a rare long wait
# is what nobody notices.
DEFAULT_OUTLIER_THRESHOLD_MINUTES = 45

# A week, because a rolling day cannot survive a weekend: Saturdays and Sundays
# here run single-digit numbers of builds, so a rolling-day percentile over a
# weekend is computed from one or two samples and says nothing either way.
DEFAULT_WINDOW_DAYS = 7

# A ceiling on the sweep. Volume only goes up: the smoke test now runs on every
# pull request, and development is getting faster, so more runs land at once. A
# sweep that reaches the periodic's timeout is killed with no output, reddening
# TestGrid without saying anything about the queue. On hitting this the sweep
# stops and reports the window it actually covered. Checked between days, never
# inside one, so a day is always whole.
DEFAULT_DEADLINE_SECONDS = 600

# Below this, a day's percentiles are printed but not allowed to trip the
# threshold on their own. Two samples can put any number at p95.
MIN_SAMPLES_FOR_DAILY_VERDICT = 5

PERCENTILE_P50 = 50
PERCENTILE_P95 = 95

SECONDS_PER_MINUTE = 60

# One `gcloud storage cat` per build, so the wall clock is round trips rather
# than bytes. Measured at roughly nine objects a second serially; sixteen at a
# time brings a seven-day window under a minute. Higher does not help -- the
# ceiling is gcloud process startup, not the network.
DEFAULT_WORKERS = 16

# No single call here should come close to these. They exist so that a hung
# read fails the run instead of hanging a cron job until the next one starts.
GCLOUD_TIMEOUT_SECONDS = 120
GCS_INDEX_TIMEOUT_SECONDS = 300
DECK_TIMEOUT_SECONDS = 120
BOSKOS_TIMEOUT_SECONDS = 30

# kubectl port-forward prints its "Forwarding from" line before the tunnel is
# reliably accepting connections, so the port is polled rather than slept on.
PORT_FORWARD_STARTUP_TIMEOUT_SECONDS = 30
PORT_FORWARD_POLL_INTERVAL_SECONDS = 0.25
PORT_FORWARD_SHUTDOWN_TIMEOUT_SECONDS = 5

# Ask the kernel for a free port rather than picking one: a fixed port collides
# with a second copy of this script, and a collision would silently read the
# other run's tunnel.
EPHEMERAL_PORT = 0
LOOPBACK = "127.0.0.1"

RFC3339_Z = "Z"
UTC_OFFSET = "+00:00"

DATE_FORMAT = "%Y-%m-%d"
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

REPORT_WIDTH = 78

# Lines the segment split up under an outlier's headline, so the two read as one
# entry rather than two.
OUTLIER_DETAIL_INDENT = 16

# The three causes of a long queue, plus the case where the pool could not be
# read. Named rather than left as prose because --json emits them and an alert
# policy filters on them, so the string is an interface and not a wording
# choice. CAPACITY is the only one that justifies onboarding a project.
CAUSE_UNKNOWN = "UNKNOWN"
CAUSE_CAPACITY = "CAPACITY"
CAUSE_CONCURRENCY_CAP = "CONCURRENCY_CAP"
CAUSE_CONTROL_PLANE = "CONTROL_PLANE"

VERDICT_OK = "OK"
VERDICT_BREACH = "BREACH"
VERDICT_UNMEASURED = "UNMEASURED"

EXIT_OK = 0
EXIT_BREACH = 1
# Distinct from both: the thresholds were not crossed because the measurement
# did not happen. A gauge that cannot read reports louder than one reading zero,
# because a silent unmeasured gauge is indistinguishable from a healthy one --
# which is the failure this whole check exists to remove.
EXIT_UNMEASURED = 2
# argparse exits 2 on a bad command line, which would be indistinguishable from
# a run that could not measure. 64 is EX_USAGE from sysexits.h.
EXIT_USAGE = 64


def _gap(start: Optional[datetime], end: Optional[datetime]) -> Optional[float]:
    """Seconds between two stamps, or None if either is missing.

    Clamped at zero, because adjacent stamps come from different writers: a run
    dispatched instantly can carry a pendingTime a few hundred milliseconds
    before its creationTimestamp, and a negative segment drags a median below
    anything a run actually experienced.
    """
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())


class Wait:
    """One run's setup time, split into the four segments it is made of.

    Setup time is everything between the ProwJob being created and the test
    being able to run: queued for a pod, the pod starting, the job's own
    preamble, then blocking on a Boskos lease. Only the first is guaranteed --
    the rest need artifacts a run may not have got far enough to write. A
    segment that could not be read is None, never zero, so a run whose lease
    time is unreadable is not counted as having leased instantly.
    """

    def __init__(
        self,
        build_id: int,
        pull: str,
        created: datetime,
        pending: datetime,
        max_concurrency: Optional[int],
        container_started: Optional[datetime] = None,
        lease_requested: Optional[datetime] = None,
        lease_acquired: Optional[datetime] = None,
    ):
        self.build_id = build_id
        self.pull = pull
        self.created = created
        self.pending = pending
        self.max_concurrency = max_concurrency
        self.container_started = container_started
        self.lease_requested = lease_requested
        self.lease_acquired = lease_acquired

    @property
    def queue_seconds(self) -> float:
        # The one segment that is never None: both stamps live in prowjob.json,
        # which a build has by virtue of being in the index at all.
        gap = _gap(self.created, self.pending)
        return 0.0 if gap is None else gap

    @property
    def pod_seconds(self) -> Optional[float]:
        return _gap(self.pending, self.container_started)

    @property
    def setup_seconds(self) -> Optional[float]:
        return _gap(self.container_started, self.lease_requested)

    @property
    def lease_seconds(self) -> Optional[float]:
        return _gap(self.lease_requested, self.lease_acquired)

    @property
    def segments(self) -> Dict[str, Optional[float]]:
        return {
            SEGMENT_QUEUE: self.queue_seconds,
            SEGMENT_POD: self.pod_seconds,
            SEGMENT_SETUP: self.setup_seconds,
            SEGMENT_LEASE: self.lease_seconds,
        }

    @property
    def total_seconds(self) -> float:
        """The segments that were measured, summed.

        An unmeasured segment is left out rather than guessed at, so on a run
        with incomplete artifacts this is a lower bound. That is the safe
        direction for a threshold: it under-reports rather than inventing time.
        """
        return sum(v for v in self.segments.values() if v is not None)

    @property
    def minutes(self) -> float:
        return self.total_seconds / SECONDS_PER_MINUTE

    @property
    def queue_minutes(self) -> float:
        return self.queue_seconds / SECONDS_PER_MINUTE

    @property
    def day(self) -> str:
        return self.created.strftime(DATE_FORMAT)


class LiveWait:
    """A run that has not got a pod yet, and how long it has been waiting.

    Prow assigns a build ID when it creates the pod, so a run still in
    `triggered` does not have one. It is identified by its pull request.
    """

    def __init__(self, pull: str, created: datetime, now: datetime):
        self.pull = pull
        self.created = created
        self.seconds = max(0.0, (now - created).total_seconds())

    @property
    def minutes(self) -> float:
        return self.seconds / SECONDS_PER_MINUTE


class LiveQueue:
    """What Deck says about this job right now.

    Two separate things, which it is worth not conflating. `waiting` is runs
    that have no pod. `running_build_ids` is runs that have one and are
    therefore holding a Boskos lease -- that second set is what a lease-holder
    list has to be compared against, and comparing against `waiting` instead
    would mark every real lease as leaked, since a waiting run holds none.
    """

    def __init__(self, waiting: List[LiveWait], running_build_ids: set):
        self.waiting = waiting
        self.running_build_ids = running_build_ids

    @property
    def running(self) -> int:
        return len(self.running_build_ids)


class PoolState:
    """How much of the Boskos pool is leased, and by whom."""

    def __init__(self, counts: Dict[str, int], owners: Dict[str, int]):
        self.counts = counts
        self.owners = owners

    @property
    def busy(self) -> int:
        return self.counts.get(BOSKOS_STATE_BUSY, 0)

    @property
    def free(self) -> int:
        return self.counts.get(BOSKOS_STATE_FREE, 0)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def in_transition(self) -> int:
        """Projects Boskos reports in neither `busy` nor `free`.

        Cleaning, dirty, leased. None of them can be handed out now, so with the
        concurrency cap equal to the pool size they are the mechanism by which a
        run blocks inside `boskosctl acquire` while the queue looks empty.
        """
        return max(0, self.total - self.busy - self.free)

    def lease_holders(self) -> List[str]:
        return sorted(k for k in self.owners if k != BOSKOS_NO_OWNER)


class Source:
    """The result of reading one source, or the reason it could not be read.

    Every source here is allowed to be missing. Conflating "read it, found
    nothing wrong" with "could not read it" is the specific mistake that turns a
    broken gauge into a green one, so the two are separate fields and the report
    prints the second rather than skipping the line.
    """

    def __init__(self, value=None, error: Optional[str] = None):
        self.value = value
        self.error = error

    @property
    def ok(self) -> bool:
        return self.error is None


def run_cmd(
    cmd: Sequence[str], timeout: int = GCLOUD_TIMEOUT_SECONDS
) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            list(cmd), capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        # 124 is what GNU timeout(1) reports, so a caller that only reads the
        # code still sees a failure rather than a success.
        return 124, "", f"timed out after {timeout}s: {' '.join(cmd)}"
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def parse_rfc3339(value: str) -> Optional[datetime]:
    """Parse a Prow timestamp into an aware UTC datetime, or None.

    Prow writes RFC 3339 with a literal Z, which fromisoformat does not accept
    before Python 3.11.
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith(RFC3339_Z):
        text = text[: -len(RFC3339_Z)] + UTC_OFFSET
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def snowflake_time(build_id: int) -> datetime:
    """The time encoded in a Prow build ID."""
    millis = (build_id >> SNOWFLAKE_TIMESTAMP_SHIFT) + SNOWFLAKE_EPOCH_MS
    return datetime.fromtimestamp(millis / MILLIS_PER_SECOND, tz=timezone.utc)


def percentile(values: Sequence[float], pct: int) -> float:
    """Linear-interpolated percentile, matching what numpy and pandas report.

    Written out rather than taken from statistics.quantiles, which needs at
    least two data points and would raise on a day with a single build.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (pct / PERCENT_SCALE)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _pull_of(prowjob: dict) -> str:
    """The pull request number this build ran against, as a string."""
    pulls = (prowjob.get("spec") or {}).get("refs", {}).get("pulls") or []
    numbers = [str(p.get("number")) for p in pulls if p.get("number") is not None]
    return ",".join(numbers)


def started_time(document: Optional[dict]) -> Optional[datetime]:
    """When the container began running, from started.json's epoch seconds."""
    if not document:
        return None
    stamp = document.get(STARTED_TIMESTAMP_KEY)
    if not isinstance(stamp, (int, float)):
        return None
    return datetime.fromtimestamp(stamp, tz=timezone.utc)


def banners(text: str) -> List[Tuple[datetime, str]]:
    """Every timestamped phase banner in a build log, in order.

    The script prints a few untimestamped ones too, like
    `=== Target Cluster Context ===`. They do not match, which is what pinning
    the grammar to the bracketed stamp is for.
    """
    found = []
    for stamp, label in BANNER_PATTERN.findall(text):
        moment = parse_rfc3339(stamp)
        if moment is not None:
            found.append((moment, label))
    return found


def lease_window(text: str) -> Tuple[Optional[datetime], Optional[datetime]]:
    """When the run asked Boskos for a project, and when it moved on.

    The second stamp is the next phase banner rather than a line of its own, so
    it bounds `boskosctl acquire` from above by the few seconds of setup that
    follow it. Above is the useful direction: the question this answers is
    whether acquiring took minutes.

    Both None when the log never reaches the lease -- a run still in its
    preamble, or killed before it got there. The release banner names Boskos
    too, so the first match is taken rather than any of them.
    """
    found = banners(text)
    for index, (moment, label) in enumerate(found):
        if BANNER_LEASE_KEYWORD in label.lower():
            following = found[index + 1][0] if index + 1 < len(found) else None
            return moment, following
    return None, None


def wait_from_prowjob(
    prowjob: dict,
    started: Optional[dict] = None,
    log_head: str = "",
) -> Optional[Wait]:
    """A Wait from a build's artifacts, or None if prowjob.json is unusable.

    prowjob.json is a snapshot initupload takes as the pod starts, so its status
    is always `pending` and pendingTime is always set; a record missing either
    stamp is malformed rather than interesting, and is dropped. The other two
    arguments are optional and each only adds segments.
    """
    status = prowjob.get("status") or {}
    spec = prowjob.get("spec") or {}
    created = parse_rfc3339((prowjob.get("metadata") or {}).get("creationTimestamp", ""))
    pending = parse_rfc3339(status.get("pendingTime", ""))
    if created is None or pending is None:
        return None
    try:
        build_id = int(status.get("build_id") or 0)
    except (TypeError, ValueError):
        build_id = 0
    max_concurrency = spec.get("max_concurrency")
    if not isinstance(max_concurrency, int):
        max_concurrency = None
    requested, acquired = lease_window(log_head)
    return Wait(
        build_id,
        _pull_of(prowjob),
        created,
        pending,
        max_concurrency,
        container_started=started_time(started),
        lease_requested=requested,
        lease_acquired=acquired,
    )


def _index_entries_from_gcs(tmpdir: str) -> Tuple[Dict[int, str], Optional[str]]:
    """Every build ID in the flat index, mapped to its artifact directory.

    Copied in bulk rather than listed and read one at a time: one `gcloud
    storage cp` of the whole prefix is a few seconds, where a thousand
    individual reads is minutes.
    """
    dest = os.path.join(tmpdir, "index")
    os.makedirs(dest, exist_ok=True)
    # The trailing wildcard matters. `cp -r` on the directory itself exits 0
    # having copied nothing.
    rc, _, err = run_cmd(
        ["gcloud", "storage", "cp", f"{GCS_BUILD_INDEX}/*", dest],
        timeout=GCS_INDEX_TIMEOUT_SECONDS,
    )
    if rc != 0:
        return {}, f"could not read the build index: {err.strip() or f'exit {rc}'}"
    return _index_entries_from_dir(dest), None


def _index_entries_from_dir(dest: str) -> Dict[int, str]:
    entries: Dict[int, str] = {}
    for name in os.listdir(dest):
        if not name.endswith(BUILD_INDEX_SUFFIX):
            continue
        try:
            build_id = int(name[: -len(BUILD_INDEX_SUFFIX)])
        except ValueError:
            continue
        try:
            path = Path(dest, name).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if path:
            entries[build_id] = path
    return entries


def _json_documents(text: str) -> List[dict]:
    """Every JSON object in a concatenation of them.

    `gcloud storage cat` given several objects writes their bodies end to end
    with nothing in between, and its `-d` separators go to stderr rather than
    stdout, so the split has to come from the JSON itself. started.json carries
    no trailing newline, so the two bodies are genuinely adjacent.
    """
    decoder = json.JSONDecoder()
    documents: List[dict] = []
    index = 0
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        try:
            document, index = decoder.raw_decode(text, index)
        except ValueError:
            break
        if isinstance(document, dict):
            documents.append(document)
    return documents


def _read_build_artifacts(path: str) -> Tuple[Optional[dict], Optional[dict]]:
    """prowjob.json and started.json, in one gcloud invocation.

    Batched because the cost here is process startup, about a second a call,
    not bytes. The return code is deliberately ignored: a missing object makes
    gcloud exit 1 having still written the ones it found, and a build that has
    not finished starting has no started.json yet.
    """
    _, out, _ = run_cmd(
        [
            "gcloud", "storage", "cat",
            f"{path}/{PROWJOB_ARTIFACT}",
            f"{path}/{STARTED_ARTIFACT}",
        ],
        timeout=GCLOUD_TIMEOUT_SECONDS,
    )
    prowjob = started = None
    for document in _json_documents(out):
        if document.get("kind") == PROWJOB_KIND:
            prowjob = document
        elif STARTED_TIMESTAMP_KEY in document:
            started = document
    return prowjob, started


def _read_log_head(path: str) -> str:
    """The first BUILD_LOG_HEAD_BYTES of build-log.txt, or "".

    The return code is ignored here too, for a different reason: a log shorter
    than the range makes gcloud exit 1 with "Download not completed" after
    writing the whole object anyway. A build still running has no log at all.
    """
    _, out, _ = run_cmd(
        [
            "gcloud", "storage", "cat",
            "-r", f"0-{BUILD_LOG_HEAD_BYTES - 1}",
            f"{path}/{BUILD_LOG_ARTIFACT}",
        ],
        timeout=GCLOUD_TIMEOUT_SECONDS,
    )
    return out


def _wait_from_gcs(path: str) -> Optional[Wait]:
    prowjob, started = _read_build_artifacts(path)
    if prowjob is None:
        return None
    # The second round trip is only worth spending once the build is known to
    # have produced a usable prowjob.json.
    return wait_from_prowjob(prowjob, started, _read_log_head(path))


class Sweep:
    """What the window walk found, and what it cost to find it.

    The cost travels with the result because it is the early warning: an
    `elapsed_seconds` climbing towards the deadline across successive TestGrid
    columns is visible long before a sweep is actually cut short.
    """

    def __init__(
        self,
        waits: List[Wait],
        builds_read: int,
        elapsed_seconds: float,
        window_start: datetime,
        truncated: bool = False,
    ):
        self.waits = waits
        self.builds_read = builds_read
        self.elapsed_seconds = elapsed_seconds
        self.window_start = window_start
        self.truncated = truncated


def collect_waits(
    window_start: datetime,
    window_end: datetime,
    workers: int = DEFAULT_WORKERS,
    from_dir: Optional[str] = None,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
) -> Source:
    """Every measurable run created inside the window, newest day first.

    The snowflake prefilter is what makes this cheap, and it is applied with
    slack in both directions because the ID encodes the *end* of the queue wait:
    a run created just inside the window that waited an hour encodes an hour
    later, and an exact comparison would drop it. The authoritative filter is
    creationTimestamp, applied after the read.

    Days are walked newest first so that a sweep cut short by the deadline loses
    the oldest context rather than today's numbers. Because every run created on
    a given day is dispatched that day or later, finishing every snowflake day
    down to D means holding every run created on D or after -- so the window
    that was actually covered is a clean date boundary, not a ragged edge.
    """
    if from_dir is not None:
        return _collect_waits_from_dir(from_dir, window_start, window_end)

    if shutil.which("gcloud") is None:
        return Source(error="gcloud is not on PATH, so the trend cannot be measured")

    began = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="pool-pressure-") as tmpdir:
        entries, err = _index_entries_from_gcs(tmpdir)
        if err:
            return Source(error=err)
        if not entries:
            return Source(error=f"the build index at {GCS_BUILD_INDEX} is empty")

        candidates: Dict[str, List[str]] = {}
        for build_id, path in entries.items():
            moment = snowflake_time(build_id)
            if (
                window_start - SNOWFLAKE_SLACK
                <= moment
                <= window_end + SNOWFLAKE_SLACK
            ):
                candidates.setdefault(moment.strftime(DATE_FORMAT), []).append(path)
        if not candidates:
            return Source(
                value=Sweep([], 0, time.monotonic() - began, window_start)
            )

        collected: List[Wait] = []
        read = 0
        done: List[str] = []
        truncated = False
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for day in sorted(candidates, reverse=True):
                if time.monotonic() - began > deadline_seconds:
                    truncated = True
                    break
                for wait in pool.map(_wait_from_gcs, sorted(candidates[day])):
                    read += 1
                    if wait is not None:
                        collected.append(wait)
                done.append(day)

    measured_start = window_start
    if truncated:
        # Everything older than the oldest day that finished is unread, so the
        # window shrinks to that boundary and the runs below it are dropped.
        # Keeping them would mix a whole day against a partial one and report
        # the result as a percentile over both.
        oldest = min(done) if done else window_end.strftime(DATE_FORMAT)
        measured_start = max(
            window_start,
            datetime.strptime(oldest, DATE_FORMAT).replace(tzinfo=timezone.utc),
        )

    waits = [w for w in collected if measured_start <= w.created <= window_end]
    return Source(
        value=Sweep(waits, read, time.monotonic() - began, measured_start, truncated)
    )


def _read_optional_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_optional_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _collect_waits_from_dir(
    from_dir: str, window_start: datetime, window_end: datetime
) -> Source:
    """The same, from captured artifacts on disk.

    Used by the tests, and by hand to reproduce a past window without spending a
    thousand GCS reads on it. Three directories named by build ID mirror the
    three objects the bucket holds; the logs are truncated to the same head the
    ranged read takes, so this exercises the parser the live path uses.
    """
    root = Path(from_dir, FIXTURE_PROWJOBS_DIR)
    if not root.is_dir():
        return Source(error=f"no {FIXTURE_PROWJOBS_DIR}/ directory under {from_dir}")
    waits: List[Wait] = []
    read = 0
    for path in sorted(root.glob("*.json")):
        prowjob = _read_optional_json(path)
        if prowjob is None:
            continue
        read += 1
        wait = wait_from_prowjob(
            prowjob,
            _read_optional_json(Path(from_dir, FIXTURE_STARTED_DIR, path.name)),
            _read_optional_text(
                Path(from_dir, FIXTURE_LOGS_DIR, path.stem + FIXTURE_LOG_SUFFIX)
            ),
        )
        if wait and window_start <= wait.created <= window_end:
            waits.append(wait)
    return Source(value=Sweep(waits, read, 0.0, window_start))


def fetch_live_queue(now: datetime, from_dir: Optional[str] = None) -> Source:
    """Runs that are queued right now and have not been given a pod.

    This is the half GCS structurally cannot see. A run waiting in `triggered`
    has written no artifacts, so during a stall the bucket stops gaining samples
    and the trend gets *quieter* as the queue gets longer.
    """
    if from_dir is not None:
        payload = Path(from_dir, "deck.json")
        if not payload.is_file():
            return Source(error=f"no deck.json under {from_dir}")
        try:
            document = json.loads(payload.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return Source(error=f"could not parse {payload}: {exc}")
    else:
        try:
            with urllib.request.urlopen(
                DECK_PROWJOBS_URL, timeout=DECK_TIMEOUT_SECONDS
            ) as response:
                document = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
            return Source(error=f"could not read Deck: {exc}")

    waiting: List[LiveWait] = []
    running: set = set()
    for item in document.get("items") or []:
        spec = item.get("spec") or {}
        status = item.get("status") or {}
        if spec.get("job") != JOB_NAME:
            continue
        # A job that has finished is neither waiting nor holding anything,
        # however it finished. Reading "no pendingTime" as "still queued" counts
        # every aborted run as a live stall, and aborted runs outnumber every
        # other state here -- enough to invent an outage that is not happening.
        #
        # Read as a flag and never as a time. On an aborted ProwJob Deck reports
        # completionTime as exactly startTime + 30:00 whatever really happened:
        # six aborted builds whose artifacts put their real pod life at 6.8 to
        # 24.1 minutes all read 30.0 here. Aborts are the largest single state
        # in this job's history, so any duration derived from this field is
        # wrong for most of the runs it covers. Durations come from artifacts.
        if status.get("completionTime"):
            continue
        if status.get("pendingTime"):
            build_id = str(status.get("build_id") or "")
            if build_id:
                running.add(build_id)
            continue
        created = parse_rfc3339((item.get("metadata") or {}).get("creationTimestamp", ""))
        if created is None:
            continue
        waiting.append(LiveWait(_pull_of(item), created, now))
    waiting.sort(key=lambda w: w.seconds, reverse=True)
    return Source(value=LiveQueue(waiting, running))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOOPBACK, EPHEMERAL_PORT))
        return sock.getsockname()[1]


def _await_port(port: int, process: subprocess.Popen) -> bool:
    deadline = time.monotonic() + PORT_FORWARD_STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(PORT_FORWARD_POLL_INTERVAL_SECONDS)
            if sock.connect_ex((LOOPBACK, port)) == 0:
                return True
        time.sleep(PORT_FORWARD_POLL_INTERVAL_SECONDS)
    return False


def _read_boskos_metric(url: str) -> Tuple[Optional[dict], Optional[str]]:
    try:
        with urllib.request.urlopen(url, timeout=BOSKOS_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8")), None
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        return None, str(exc)


def fetch_pool_state(
    via: str, context: str, from_dir: Optional[str] = None
) -> Source:
    """How many pool projects are leased, and by which runs.

    Boskos is the only source that can tell a full pool from an idle one, which
    is the whole difference between "onboard another project" and
    "oss-test-infra#2666 again". Without it a breach has no verdict, so its
    absence is reported rather than defaulted either way.
    """
    if from_dir is not None:
        payload = Path(from_dir, "boskos.json")
        if not payload.is_file():
            return Source(error=f"no boskos.json under {from_dir}")
        try:
            document = json.loads(payload.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return Source(error=f"could not parse {payload}: {exc}")
        return Source(value=_pool_state_from(document))

    if via == BOSKOS_VIA_NONE:
        return Source(error="Boskos was not queried (--boskos-via none)")

    if via == BOSKOS_VIA_HTTP:
        document, err = _read_boskos_metric(BOSKOS_IN_CLUSTER_URL)
        if err:
            return Source(error=f"could not reach Boskos in-cluster: {err}")
        return Source(value=_pool_state_from(document))

    if shutil.which(KUBECTL_BINARY) is None:
        return Source(error="kubectl is not on PATH, so Boskos cannot be reached")

    port = _free_port()
    process = subprocess.Popen(
        [
            KUBECTL_BINARY,
            "--context",
            context,
            "-n",
            BOSKOS_NAMESPACE,
            "port-forward",
            BOSKOS_SERVICE,
            f"{port}:{BOSKOS_SERVICE_PORT}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        if not _await_port(port, process):
            detail = (process.stderr.read() or "").strip() if process.stderr else ""
            return Source(
                error=f"could not port-forward to Boskos: {detail or 'tunnel never opened'}"
            )
        document, err = _read_boskos_metric(
            f"http://{LOOPBACK}:{port}{BOSKOS_METRIC_PATH}"
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=PORT_FORWARD_SHUTDOWN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
    if err:
        return Source(error=f"could not read the Boskos metric: {err}")
    return Source(value=_pool_state_from(document))


def _pool_state_from(document: dict) -> PoolState:
    counts = {
        str(k): int(v)
        for k, v in (document.get("current") or {}).items()
        if isinstance(v, int)
    }
    owners = {
        str(k): int(v)
        for k, v in (document.get("owner") or {}).items()
        if isinstance(v, int)
    }
    return PoolState(counts, owners)


class DayRow:
    def __init__(self, day: str, waits: List[Wait]):
        minutes = [w.minutes for w in waits]
        self.day = day
        self.count = len(waits)
        self.p50 = percentile(minutes, PERCENTILE_P50)
        self.p95 = percentile(minutes, PERCENTILE_P95)
        self.worst = max(minutes) if minutes else 0.0
        concurrencies = {w.max_concurrency for w in waits if w.max_concurrency}
        self.max_concurrency = max(concurrencies) if concurrencies else None

    def breached(self, p50_limit: float, p95_limit: float) -> bool:
        if self.count < MIN_SAMPLES_FOR_DAILY_VERDICT:
            return False
        return self.p50 > p50_limit or self.p95 > p95_limit


def daily_rows(waits: List[Wait]) -> List[DayRow]:
    by_day: Dict[str, List[Wait]] = {}
    for wait in waits:
        by_day.setdefault(wait.day, []).append(wait)
    return [DayRow(day, by_day[day]) for day in sorted(by_day)]


def outliers(waits: List[Wait], threshold_minutes: float) -> List[Wait]:
    over = [w for w in waits if w.minutes > threshold_minutes]
    over.sort(key=lambda w: w.total_seconds, reverse=True)
    return over


def _segment_minutes(wait: Wait) -> Dict[str, Optional[float]]:
    return {
        key: None if value is None else round(value / SECONDS_PER_MINUTE, 1)
        for key, value in wait.segments.items()
    }


def segment_breakdown(waits: List[Wait]) -> List[dict]:
    """The median of each segment, and how many runs it was measured on.

    The count is not decoration. A segment whose source has moved goes quiet
    rather than wrong, and a median over the few runs that still parse reads
    exactly like a healthy number -- so under SEGMENT_MIN_COVERAGE the median is
    withheld and the row says it was not measured.
    """
    rows = []
    for key, label in SEGMENT_LABELS:
        present = [
            w.segments[key] / SECONDS_PER_MINUTE
            for w in waits
            if w.segments[key] is not None
        ]
        covered = bool(present) and len(present) >= len(waits) * SEGMENT_MIN_COVERAGE
        rows.append(
            {
                "segment": key,
                "label": label,
                "median_minutes": (
                    round(percentile(present, PERCENTILE_P50), 1) if covered else None
                ),
                "measured": len(present),
                "runs": len(waits),
            }
        )
    return rows


def leaked_leases(pool: Optional[PoolState], queue: Optional[LiveQueue]) -> List[str]:
    """Lease holders that no running prowjob accounts for.

    A leaked lease occupies a project as surely as a running test does, and from
    outside the pool looks equally full either way. Without this the verdict
    would read a leak as demand and recommend buying capacity to replace
    capacity that was never released.

    Needs both sources: with no list of running jobs to compare against, every
    holder looks orphaned.
    """
    if pool is None or queue is None:
        return []
    orphaned = []
    for holder in pool.lease_holders():
        _, _, build_id = holder.rpartition("-")
        if build_id and build_id not in queue.running_build_ids:
            orphaned.append(holder)
    return orphaned


def latest_max_concurrency(waits: List[Wait]) -> Optional[int]:
    """The concurrency cap the most recent run in the window ran under.

    Read per build rather than hardcoded: it has moved four times in three
    weeks here (1, 2, 6, 10), and a stale copy of it would misdiagnose every
    breach it was consulted on.
    """
    with_cap = [w for w in waits if w.max_concurrency]
    if not with_cap:
        return None
    return max(with_cap, key=lambda w: w.created).max_concurrency


def _fmt(minutes: float) -> str:
    return f"{minutes:.1f}"


def summarise(
    window_start: datetime,
    window_end: datetime,
    p50_limit: float,
    p95_limit: float,
    outlier_limit: float,
    trend: Source,
    live: Source,
    pool: Source,
) -> dict:
    """Everything the check concluded, as data.

    Split from the rendering below so the same run can be printed for a person
    and emitted as JSON for whatever delivers the alert. The alert is the reason
    the split matters: a notification that says only "exit 1" makes the reader
    go and run the check again, so the numbers have to travel with it.
    """
    sweep: Optional[Sweep] = trend.value if trend.ok else None
    waits = sweep.waits if sweep else []
    rows = daily_rows(waits)
    breached_days = [r for r in rows if r.breached(p50_limit, p95_limit)]
    minutes = [w.minutes for w in waits]

    queue: Optional[LiveQueue] = live.value if live.ok else None
    live_breach = [w for w in queue.waiting if w.minutes > p95_limit] if queue else []

    pool_state: Optional[PoolState] = pool.value if pool.ok else None
    concurrency = latest_max_concurrency(waits)

    # The cap and the pool size are set in two different repositories, so
    # nothing keeps them in step. A cap below the pool leaves the extra projects
    # unreachable: they sit free while runs queue, which from outside reads
    # exactly like a control-plane stall.
    stranded = (
        max(0, pool_state.total - concurrency)
        if pool_state is not None and concurrency is not None
        else 0
    )

    breached = bool(breached_days or live_breach)
    if not trend.ok and not breached:
        verdict, exit_code = VERDICT_UNMEASURED, EXIT_UNMEASURED
    elif not breached:
        verdict, exit_code = VERDICT_OK, EXIT_OK
    else:
        verdict, exit_code = VERDICT_BREACH, EXIT_BREACH

    # Only diagnosed when there is something to diagnose. `cause` answers "why
    # are runs waiting", and on a green run the answer is that they are not --
    # emitting CONCURRENCY_CAP beside verdict OK reads as a live problem to
    # anything filtering on the label. What the cap and the pool size are is
    # still reported, under `max_concurrency` and `pool.stranded`.
    cause_label, cause_text = (
        cause(pool_state, queue, concurrency) if breached else (None, [])
    )

    return {
        "job": JOB_NAME,
        "window_start": window_start.strftime(TIMESTAMP_FORMAT),
        "window_end": window_end.strftime(TIMESTAMP_FORMAT),
        "thresholds": {
            "p50_minutes": p50_limit,
            "p95_minutes": p95_limit,
            "outlier_minutes": outlier_limit,
        },
        "trend": {
            "read": trend.ok,
            "error": trend.error,
            "runs": len(waits),
            "p50_minutes": round(percentile(minutes, PERCENTILE_P50), 1),
            "p95_minutes": round(percentile(minutes, PERCENTILE_P95), 1),
            "worst_minutes": round(max(minutes), 1) if minutes else 0.0,
            # What the sweep actually covered, which is the window it was asked
            # for unless the deadline cut it short, plus what it cost to do.
            "window_start": (
                sweep.window_start if sweep else window_start
            ).strftime(TIMESTAMP_FORMAT),
            "truncated": bool(sweep and sweep.truncated),
            "builds_read": sweep.builds_read if sweep else 0,
            "elapsed_seconds": round(sweep.elapsed_seconds, 1) if sweep else 0.0,
            "segments": segment_breakdown(waits),
            "days": [
                {
                    "day": r.day,
                    "runs": r.count,
                    "p50_minutes": round(r.p50, 1),
                    "p95_minutes": round(r.p95, 1),
                    "worst_minutes": round(r.worst, 1),
                    "max_concurrency": r.max_concurrency,
                    "breached": r.breached(p50_limit, p95_limit),
                    "judged": r.count >= MIN_SAMPLES_FOR_DAILY_VERDICT,
                }
                for r in rows
            ],
            "breached_days": [r.day for r in breached_days],
        },
        "outliers": [
            {
                "minutes": round(w.minutes, 1),
                "created": w.created.strftime(TIMESTAMP_FORMAT),
                "pull": w.pull,
                "build_id": str(w.build_id),
                # "175 minutes, all of it in Prow's queue" and "175 minutes, 9
                # of them waiting for a project" are different incidents with
                # different remedies, so the split travels with each one.
                "segments": _segment_minutes(w),
            }
            for w in outliers(waits, outlier_limit)
        ],
        "queue": {
            "read": live.ok,
            "error": live.error,
            "waiting": len(queue.waiting) if queue else None,
            "running": queue.running if queue else None,
            "over_threshold": len(live_breach),
            "waiting_runs": [
                {"minutes": round(w.minutes, 1), "pull": w.pull}
                for w in (queue.waiting if queue else [])
            ],
        },
        "pool": {
            "read": pool.ok,
            "error": pool.error,
            "busy": pool_state.busy if pool_state else None,
            "free": pool_state.free if pool_state else None,
            "total": pool_state.total if pool_state else None,
            "in_transition": pool_state.in_transition if pool_state else None,
            "stranded": stranded,
        },
        "max_concurrency": concurrency,
        "leaked_leases": leaked_leases(pool_state, queue),
        "cause": cause_label,
        "cause_text": cause_text,
        "breached": breached,
        "verdict": verdict,
        "exit_code": exit_code,
    }


def render(summary: dict) -> str:
    """The human report, from the same data --json emits."""
    trend = summary["trend"]
    queue = summary["queue"]
    pool = summary["pool"]
    limits = summary["thresholds"]
    out: List[str] = []

    out.append("=" * REPORT_WIDTH)
    out.append(f" Evaluation pool queue wait: {summary['job']}")
    out.append(f" {summary['window_start']}  ->  {summary['window_end']}")
    out.append("=" * REPORT_WIDTH)

    if not trend["read"]:
        out.append(f"\n[?] Trend not measured: {trend['error']}")

    if trend["truncated"]:
        out.append(
            f"\n[!] The sweep ran out of time and covers {trend['window_start']}"
            " onward,\n    not the whole window above. Older days are missing"
            " from the table, not empty."
        )

    if trend["days"]:
        out.append(
            f"\nPer-day wait in minutes"
            f"  (thresholds: p50 > {_fmt(limits['p50_minutes'])},"
            f" p95 > {_fmt(limits['p95_minutes'])})\n"
        )
        out.append(f"{'day':<12}{'runs':>6}{'p50':>9}{'p95':>9}{'worst':>9}{'conc':>7}   ")
        for row in trend["days"]:
            concurrency = "-" if row["max_concurrency"] is None else str(row["max_concurrency"])
            flag = "  BREACH" if row["breached"] else ""
            thin = "" if row["judged"] else "  (too few runs to judge)"
            out.append(
                f"{row['day']:<12}{row['runs']:>6}{_fmt(row['p50_minutes']):>9}"
                f"{_fmt(row['p95_minutes']):>9}{_fmt(row['worst_minutes']):>9}"
                f"{concurrency:>7}{flag}{thin}"
            )
        out.append(
            f"\n{'window':<12}{trend['runs']:>6}{_fmt(trend['p50_minutes']):>9}"
            f"{_fmt(trend['p95_minutes']):>9}{_fmt(trend['worst_minutes']):>9}"
        )
        if trend["elapsed_seconds"]:
            out.append(
                f"{trend['builds_read']} builds read in "
                f"{trend['elapsed_seconds']}s."
            )
    elif trend["read"]:
        out.append("\nNo runs were created in this window.")

    if trend["runs"]:
        out.append("\nWhere the setup time goes (window median)\n")
        for row in trend["segments"]:
            value = (
                SEGMENT_UNMEASURED
                if row["median_minutes"] is None
                else f"{_fmt(row['median_minutes'])} min"
            )
            out.append(
                f"  {row['label']:<{SEGMENT_LABEL_WIDTH}}"
                f"{value:>{SEGMENT_VALUE_WIDTH}}"
                f"   ({row['measured']} of {row['runs']})"
            )

    over = summary["outliers"]
    out.append(
        f"\nIndividual runs that waited over "
        f"{_fmt(limits['outlier_minutes'])} minutes: {len(over)}"
    )
    if over:
        out.append("A long wait among a handful of runs moves no percentile, so these are")
        out.append("listed one by one rather than left to the table above.\n")
        for wait in over:
            out.append(
                f"  {_fmt(wait['minutes']):>8} min  {wait['created']}  "
                f"PR {wait['pull'] or '?':<6}  build {wait['build_id']}"
            )
            out.append(
                " " * OUTLIER_DETAIL_INDENT
                + "  ".join(
                    f"{key} {'-' if value is None else _fmt(value)}"
                    for key, value in wait["segments"].items()
                )
            )

    if not queue["read"]:
        out.append(f"\nQueued right now: not known -- {queue['error']}")
    else:
        out.append(
            f"\nQueued right now: {queue['waiting']} run(s) with no pod yet, "
            f"{queue['running']} running"
        )
        for wait in queue["waiting_runs"]:
            marker = "  BREACH" if wait["minutes"] > limits["p95_minutes"] else ""
            out.append(f"  {_fmt(wait['minutes']):>8} min  PR {wait['pull'] or '?'}{marker}")
        out.append(
            f"  (Deck keeps {int(DECK_HORIZON.total_seconds() // SECONDS_PER_HOUR)}h of history;"
            " a run older than that is invisible here.)"
        )

    if not pool["read"]:
        out.append(f"\nPool state: not known -- {pool['error']}")
    else:
        out.append(
            f"\nPool state: {pool['busy']} leased, {pool['free']} free, "
            f"{pool['total']} projects total"
        )
        if pool["in_transition"]:
            out.append(
                f"  {pool['in_transition']} project(s) in neither state -- cleaning,"
                " dirty, or mid-release,"
            )
            out.append("  and so not leasable right now.")
    concurrency = summary["max_concurrency"]
    out.append(
        "Concurrency cap: "
        + ("not known" if concurrency is None else str(concurrency))
        + "  (max_concurrency, read from the newest run in the window)"
    )

    if pool["stranded"] > 0:
        out.append(
            f"\n[!] The cap ({concurrency}) is below the pool ({pool['total']}): "
            f"{pool['stranded']} project(s) can never be leased."
        )

    orphans = summary["leaked_leases"]
    if orphans:
        out.append(f"\n[!] {len(orphans)} lease(s) held by runs Deck does not know about.")
        out.append("    A leaked lease occupies a project exactly as a running test does,")
        out.append("    so the pool reads as full without the demand to justify it.")
        for holder in orphans:
            out.append(f"      {holder}")

    out.append("\n" + "-" * REPORT_WIDTH)
    if summary["verdict"] == VERDICT_UNMEASURED:
        out.append("COULD NOT MEASURE. The thresholds were not crossed because the wait")
        out.append("was never read. This is not a green run.")
        out.append("-" * REPORT_WIDTH)
        return "\n".join(out)

    if summary["verdict"] == VERDICT_OK:
        out.append("WITHIN THRESHOLD. No action.")
        out.append("-" * REPORT_WIDTH)
        return "\n".join(out)

    if trend["breached_days"]:
        out.append(
            f"THRESHOLD BREACHED on {len(trend['breached_days'])} day(s): "
            + ", ".join(trend["breached_days"])
        )
    if queue["over_threshold"]:
        out.append(
            f"QUEUE IS LONG RIGHT NOW: {queue['over_threshold']} run(s) waiting over "
            f"{_fmt(limits['p95_minutes'])} minutes."
        )

    out.append("")
    out.extend(summary["cause_text"])
    out.append("")
    out.append("Expansion is a human decision. This check does not provision anything.")
    out.append("-" * REPORT_WIDTH)
    return "\n".join(out)


def _cap_at_pool_caveat(
    pool_state: PoolState, concurrency: Optional[int]
) -> List[str]:
    """The extra sentence a saturated cap earns, if it is saturated.

    Appended to whatever cause was diagnosed rather than being a cause of its
    own: the labels are what the periodic and any alert policy filter on, and a
    fifth one would send readers to a remedy that does not exist.

    With the cap at or above the pool, Prow admits enough pods to claim every
    project, so nothing is held in reserve. One project cleaning, dirty, or
    leaked is then a pod that starts on time and blocks inside `boskosctl
    acquire` until its timeout. The Boskos acquire segment is where that shows.
    """
    if concurrency is None or concurrency < pool_state.total:
        return []
    lines = [
        "",
        f"Note: the cap ({concurrency}) is at or above the pool"
        f" ({pool_state.total}), so no",
        "project is held back for a late pod. Any project not free is a pod that"
        " waits",
        "inside `boskosctl acquire` rather than in Prow's queue -- read the Boskos",
        "acquire segment above, not the queue segment.",
    ]
    if pool_state.in_transition:
        lines.append(
            f"{pool_state.in_transition} project(s) are in neither state right now."
        )
    return lines


def cause(
    pool_state: Optional[PoolState],
    queue: Optional[LiveQueue],
    concurrency: Optional[int],
) -> Tuple[str, List[str]]:
    """Why runs are waiting, and therefore what to do about it.

    Returns the machine-readable label and the prose, together, so the two
    cannot drift: --json emits the label and an alert policy branches on it,
    while the report prints the prose underneath.

    Three causes look identical from the outside -- runs queue, nothing fails --
    and they have three different remedies, one of which costs a project.

    Ordered by what can be ruled out cheapest. A full pool is unambiguous. A
    pool with free projects that nothing can reach is the concurrency cap, and
    checking that before blaming the control plane matters because the two are
    indistinguishable in the wait times alone: in both, runs sit in `triggered`
    while projects sit free.
    """
    if pool_state is None:
        return CAUSE_UNKNOWN, [
            "Cause unknown: the pool's occupancy could not be read, and a long wait",
            "means opposite things depending on it. Do not onboard a project on the",
            "strength of this run alone -- the Boskos acquire segment above breaks",
            "the tie: late lease, real contention; prompt lease, control plane.",
        ]

    if pool_state.free == 0:
        return CAUSE_CAPACITY, [
            "CAPACITY. Every project was leased while runs were waiting, so the",
            "queue is real demand. Onboard the next project, per the pool runbook:",
            "docs/site/src/content/docs/deploy/ci-pool-projects.md",
        ] + _cap_at_pool_caveat(pool_state, concurrency)

    if concurrency is not None and pool_state.total > concurrency:
        lines = [
            f"CONCURRENCY CAP, not capacity. {pool_state.free} project(s) were free",
            f"while runs waited. max_concurrency is {concurrency} against a pool of",
            f"{pool_state.total}, so what holds the queue back is the cap rather than",
            "the number of projects.",
        ]
        if queue is not None and queue.running >= concurrency:
            lines.append(
                f"Deck confirms it: {queue.running} run(s) running, which is the cap."
            )
        lines += [
            "",
            "Raise the cap in oss-test-infra's kube-agents presubmit to the number of",
            "leasable projects before onboarding anything: the projects to absorb this",
            "queue already exist and are being paid for.",
        ]
        return CAUSE_CONCURRENCY_CAP, lines

    return CAUSE_CONTROL_PLANE, [
        f"CONTROL PLANE, not capacity. {pool_state.free} project(s) were free while",
        "runs were waiting, and the concurrency cap was not what held them back, so",
        "there was nothing for them to wait for. This is the shape of",
        "GoogleCloudPlatform/oss-test-infra#2666, where prowjobs sat in `triggered`",
        "with an idle build cluster. Onboarding another project would spend money",
        "and change nothing. The Boskos acquire segment above confirms it: late",
        "lease, real contention; prompt lease, control plane.",
    ] + _cap_at_pool_caveat(pool_state, concurrency)


class _Parser(argparse.ArgumentParser):
    """An ArgumentParser that exits EXIT_USAGE rather than argparse's own 2."""

    def error(self, message: str):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def measure(
    window_days: int = DEFAULT_WINDOW_DAYS,
    as_of: Optional[datetime] = None,
    p50_limit: float = DEFAULT_P50_THRESHOLD_MINUTES,
    p95_limit: float = DEFAULT_P95_THRESHOLD_MINUTES,
    outlier_limit: float = DEFAULT_OUTLIER_THRESHOLD_MINUTES,
    boskos_via: str = BOSKOS_VIA_KUBECTL,
    context: str = PROW_BUILD_CLUSTER_CONTEXT,
    workers: int = DEFAULT_WORKERS,
    from_dir: Optional[str] = None,
    as_json: bool = False,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
) -> int:
    window_end = as_of or datetime.now(timezone.utc)
    window_start = window_end - timedelta(days=window_days)

    trend = collect_waits(window_start, window_end, workers, from_dir, deadline_seconds)

    # Deck and Boskos both answer about *now*, so replaying a past window with
    # them attached would pair an old trend with today's queue and read as if
    # they described the same moment.
    if as_of is not None and from_dir is None:
        replay = Source(error="not read: --as-of replays a past window, and this is live")
        live, pool = replay, replay
    else:
        live = fetch_live_queue(window_end, from_dir)
        pool = fetch_pool_state(boskos_via, context, from_dir)

    summary = summarise(
        window_start,
        window_end,
        p50_limit,
        p95_limit,
        outlier_limit,
        trend,
        live,
        pool,
    )

    if as_json:
        # The rendered report travels inside the payload rather than beside it.
        # Whatever delivers the alert then has both without running the check
        # twice, and a seven-day window costs about a minute a run.
        print(json.dumps({**summary, "report": render(summary)}, indent=1))
    else:
        print(render(summary))
    return summary["exit_code"]


def main() -> int:
    parser = _Parser(
        description=(
            "Measure how long CI runs wait for a pool project, and report whether a "
            "long wait is the pool being full or the Prow control plane stalling."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exit codes:\n"
            "  0  every wait measured, nothing over threshold\n"
            "  1  a threshold was crossed -- read the cause line before acting\n"
            "  2  the wait could not be measured; not a green run\n"
            " 64  bad command line\n"
        ),
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=DEFAULT_WINDOW_DAYS,
        help=(
            f"days of history to measure (default: {DEFAULT_WINDOW_DAYS}). A rolling "
            "day cannot survive a weekend here, where Saturdays run single-digit "
            "numbers of builds."
        ),
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help=(
            "end the window at this UTC time (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ) "
            "instead of now, to replay a past day. Deck and Boskos describe the "
            "present, so they are not read in this mode."
        ),
    )
    parser.add_argument(
        "--p50-threshold-minutes",
        type=float,
        default=DEFAULT_P50_THRESHOLD_MINUTES,
        help=f"median wait that counts as a breach (default: {DEFAULT_P50_THRESHOLD_MINUTES})",
    )
    parser.add_argument(
        "--p95-threshold-minutes",
        type=float,
        default=DEFAULT_P95_THRESHOLD_MINUTES,
        help=f"95th-percentile wait that counts as a breach (default: {DEFAULT_P95_THRESHOLD_MINUTES})",
    )
    parser.add_argument(
        "--outlier-threshold-minutes",
        type=float,
        default=DEFAULT_OUTLIER_THRESHOLD_MINUTES,
        help=(
            "list every run that waited longer than this, whatever the percentiles "
            f"did (default: {DEFAULT_OUTLIER_THRESHOLD_MINUTES})"
        ),
    )
    parser.add_argument(
        "--boskos-via",
        choices=BOSKOS_VIA_CHOICES,
        default=BOSKOS_VIA_KUBECTL,
        help=(
            "how to reach the Boskos metric endpoint: port-forward with kubectl "
            "(default), direct HTTP when running inside the build cluster, or skip "
            "it and report the cause as unknown"
        ),
    )
    parser.add_argument(
        "--context",
        default=PROW_BUILD_CLUSTER_CONTEXT,
        help=f"kubectl context for the Prow build cluster (default: {PROW_BUILD_CLUSTER_CONTEXT})",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"parallel GCS reads (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--deadline-seconds",
        type=float,
        default=DEFAULT_DEADLINE_SECONDS,
        help=(
            "stop sweeping after this long and report the window actually "
            f"covered (default: {DEFAULT_DEADLINE_SECONDS}). Days are walked "
            "newest first, so what a short sweep loses is the oldest history."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "print the findings as a JSON object instead of the table, with the "
            "rendered table under `report`. The exit code is unchanged. Intended "
            "for whatever delivers the alert: `cause` is one of "
            f"{CAUSE_CAPACITY}, {CAUSE_CONCURRENCY_CAP}, {CAUSE_CONTROL_PLANE}, "
            f"{CAUSE_UNKNOWN}, and only {CAUSE_CAPACITY} justifies onboarding."
        ),
    )
    parser.add_argument(
        "--from-dir",
        default=None,
        help=(
            "read captured artifacts from this directory instead of the network. "
            "Expects prowjobs/*.json, and optionally deck.json and boskos.json."
        ),
    )
    args = parser.parse_args()

    as_of = None
    if args.as_of:
        as_of = parse_rfc3339(args.as_of)
        if as_of is None:
            try:
                as_of = datetime.strptime(args.as_of, DATE_FORMAT).replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                parser.error(f"--as-of is not a date or RFC 3339 timestamp: {args.as_of}")
    if args.window_days < 1:
        parser.error("--window-days must be at least 1")
    if args.deadline_seconds <= 0:
        parser.error("--deadline-seconds must be positive")
    # ThreadPoolExecutor raises on max_workers < 1, and the sweep catches its own
    # errors, so without this the run reports "could not measure" -- a usage
    # mistake wearing the exit code that means the pool could not be read.
    if args.workers < 1:
        parser.error("--workers must be at least 1")

    return measure(
        window_days=args.window_days,
        as_of=as_of,
        p50_limit=args.p50_threshold_minutes,
        p95_limit=args.p95_threshold_minutes,
        outlier_limit=args.outlier_threshold_minutes,
        boskos_via=args.boskos_via,
        context=args.context,
        workers=args.workers,
        from_dir=args.from_dir,
        as_json=args.json,
        deadline_seconds=args.deadline_seconds,
    )


if __name__ == "__main__":
    sys.exit(main())
