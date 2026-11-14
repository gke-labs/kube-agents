"""One run on the sized test fleet: reset the install, a burst window, a soak window, and the verdict.

docs/designs/sized-test-fleet.md, "The nightly run" and "The verdict", is the
design. The burst window holds the first-run flow and, beside it, every other
audit once on a 5-minute stagger; it lasts 1 hour nominal, to the last audit's
end, and at most 3 hours. The soak window is the next hour. The verdict is red
when anything crashed, an audit did not end, the first-run flow did not end, or
the fleet was not in its designed state.

The decisions are pure functions over what the run read, so they are tested on
their own; :func:`run` does the reading and the waiting.
"""

from __future__ import annotations

import ast
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from kube_agents_bench.scale import collect, kube, operations, sized_records

LOG = logging.getLogger(__name__)
REPO = Path(__file__).resolve().parents[3]
JOBS_FILE = REPO / "agents" / "platform" / "cron" / "jobs.json"
OOBE_FILE = REPO / "agents" / "chat" / "scripts" / "oobe.py"
AUDIT_SKILL = "fleet-audit"
FIRST_RUN_NAME = "FIRST_RUN_AUDITS"
PLATFORM_HOME = "/opt/data/profiles/platform"
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600
STAGGER_SECONDS = 5 * SECONDS_PER_MINUTE
BURST_MIN_SECONDS = 1 * SECONDS_PER_HOUR
BURST_MAX_SECONDS = 3 * SECONDS_PER_HOUR
SOAK_SECONDS = 1 * SECONDS_PER_HOUR
POLL_SECONDS = 60
# As oobe.py does for its own marks: a mark not claimed within START_LIMIT_SECONDS is
# made again, at most MAX_MARK_ATTEMPTS times. In the first run, one audit's mark sat
# unclaimed for 1 h 50 m and held the burst window open the whole time.
START_LIMIT_SECONDS = 10 * SECONDS_PER_MINUTE
MAX_MARK_ATTEMPTS = 5
UNCLAIMED = "unclaimed"
# Right after a reset the cron store has no executions table until the first tick.
NO_TABLE = "no such table"
# A run that ended, whatever its outcome; the verdict reads the outcome separately.
COMPLETED = "completed"
ENDED = (COMPLETED, "failed", "error", "unknown", "skipped")
OOM_KILLED = "OOMKilled"
ISO = "%Y-%m-%dT%H:%M:%SZ"
LOG_READ_LIMIT = 50


def audits(jobs_file: Path = JOBS_FILE) -> tuple[str, ...]:
    """The audits: the Platform Agent's cron jobs that carry the fleet-audit skill, in roster order."""
    data = json.loads(jobs_file.read_text())
    jobs = data["jobs"] if isinstance(data, dict) else data
    return tuple(j["id"] for j in jobs if AUDIT_SKILL in (j.get("skills") or []))


def first_run_audits(oobe_file: Path = OOBE_FILE) -> tuple[str, ...]:
    """``FIRST_RUN_AUDITS`` as oobe.py declares it, read from the source so the two cannot drift."""
    for node in ast.parse(oobe_file.read_text()).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == FIRST_RUN_NAME for t in node.targets):
            return tuple(ast.literal_eval(node.value))
    raise ValueError(f"{FIRST_RUN_NAME} not found in {oobe_file}")


def stagger(others: tuple[str, ...], step: int = STAGGER_SECONDS) -> list[tuple[int, str]]:
    """When to mark each audit the first-run flow does not run: one every ``step``, from ``step`` after the start."""
    return [((i + 1) * step, job) for i, job in enumerate(others)]


def to_remark(marks: dict[str, tuple[float, int]], statuses: dict[str, str], now: float) -> list[str]:
    """The marked audits still unclaimed past START_LIMIT_SECONDS that have attempts left."""
    return [job for job, (at, attempts) in marks.items()
            if statuses.get(job, UNCLAIMED) == UNCLAIMED and now - at >= START_LIMIT_SECONDS
            and attempts < MAX_MARK_ATTEMPTS]


def burst_over(elapsed: float, statuses: dict[str, str], wanted: tuple[str, ...]) -> bool:
    """The burst window ends at 3 hours, or after 1 hour once every audit's run has ended."""
    if elapsed >= BURST_MAX_SECONDS:
        return True
    return elapsed >= BURST_MIN_SECONDS and all(statuses.get(job) in ENDED for job in wanted)


@dataclass
class PodState:
    restarts: int = 0
    oom_killed: bool = False


@dataclass
class RunRecord:
    """What the run read, for the verdict."""
    started: float
    burst_seconds: float = 0.0
    statuses: dict[str, str] = field(default_factory=dict)
    # The audits' statuses when the burst window ended: the verdict judges on these, so an
    # audit that ends only during the soak window still fails.
    burst_statuses: dict[str, str] = field(default_factory=dict)
    job_seconds: dict[str, float] = field(default_factory=dict)
    pods_before: dict[str, PodState] = field(default_factory=dict)
    pods_after: dict[str, PodState] = field(default_factory=dict)
    sandbox_changed: list[str] = field(default_factory=list)
    node_oom_kills: list[str] = field(default_factory=list)
    fleet_check: dict[str, Any] = field(default_factory=dict)
    windows: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def verdict(record: RunRecord, wanted: tuple[str, ...], first_run: tuple[str, ...]) -> dict[str, Any]:
    """The four checks of the design's verdict, each with what failed it."""
    crashes = []
    for pod, after in record.pods_after.items():
        before = record.pods_before.get(pod, PodState())
        if after.restarts > before.restarts:
            crashes.append(f"{pod}: {after.restarts - before.restarts} restart(s)")
        if after.oom_killed and not before.oom_killed:
            crashes.append(f"{pod}: OOMKilled")
    crashes += [f"SandboxChanged: {e}" for e in record.sandbox_changed]
    crashes += [f"node OOM kill: {e}" for e in record.node_oom_kills]

    statuses = record.burst_statuses or record.statuses

    def not_in(jobs: tuple[str, ...], accepted: tuple[str, ...]) -> list[str]:
        return [f"{job}: {statuses.get(job, 'not run')}" for job in jobs if statuses.get(job) not in accepted]

    checks = {
        "no_crash": crashes,
        # Every audit's run completed by the end of the burst window, 3 hours at most.
        "every_audit_finishes": not_in(wanted, (COMPLETED,)),
        # The first-run flow ends when each of its audits has an ended run, whatever the outcome
        # (a failed one already fails the check above).
        "first_run_flow_ends": not_in(first_run, ENDED),
        # The fleet was in its designed state. Faults the run recovered from are notes, not this.
        "run_sound": list(record.fleet_check.get("findings", ["the fleet was not checked"])),
    }
    return {
        "green": not any(checks.values()),
        "checks": {name: {"passed": not failures, "failures": failures} for name, failures in checks.items()},
        "burst_seconds": round(record.burst_seconds),
        "audit_seconds": {job: round(s) for job, s in record.job_seconds.items()},
        "errors": list(record.errors),
    }


def pod_states(context: str, namespace: str) -> dict[str, PodState]:
    """Each install pod's restart count and whether a container's last end was an OOM kill."""
    out = {}
    for pod in kube.kubectl_json(context, "get", "pods", "-n", namespace)["items"]:
        name = pod["metadata"]["name"]
        statuses = pod.get("status", {}).get("containerStatuses", []) + pod.get("status", {}).get("initContainerStatuses", [])
        out[name] = PodState(
            restarts=sum(s.get("restartCount", 0) for s in statuses),
            oom_killed=any(s.get("lastState", {}).get("terminated", {}).get("reason") == OOM_KILLED for s in statuses),
        )
    return out


def log_lines(project: str, query: str, since: float) -> list[str]:
    """Cloud Logging lines for ``query`` since ``since``: events expire from the API in about an hour."""
    stamp = datetime.fromtimestamp(since, timezone.utc).strftime(ISO)
    out = kube.gcloud("logging", "read", f'{query} AND timestamp>="{stamp}"', f"--limit={LOG_READ_LIMIT}",
                      "--format=value(timestamp,textPayload,jsonPayload.message)", project=project)
    return [line for line in out.splitlines() if line.strip()]


def _snapshot(install: collect.Install | None, errors: list[str]) -> collect.Snapshot | None:
    if install is None:
        return None
    try:
        snap = install.snapshot(["litellm"])
    except kube.CommandError as exc:
        errors.append(f"could not read LiteLLM's counters: {exc}")
        return None
    # A missing pod, port or scrape is reported inside the snapshot, not raised: without this the
    # window would show no tokens and $0 of spend with nothing to say the read failed.
    errors.extend(f"LiteLLM counters: {e}" for e in snap.errors)
    return snap


def _window(install: collect.Install | None, start: float, end: float, before: collect.Snapshot | None,
            after: collect.Snapshot | None, errors: list[str]) -> dict[str, Any]:
    if install is None or before is None or after is None:
        return {}
    return sized_records.safe(lambda: sized_records.window(install, start, end, before, after), errors, "window")


def run(
    *, project: str, host_cluster: str, host_context: str, namespace: str,
    check_fleet: Callable[[], dict[str, Any]], soak_seconds: int = SOAK_SECONDS,
    install: collect.Install | None = None,
    clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
) -> tuple[RunRecord, dict[str, Any]]:
    """Reset the install, run the burst and soak windows, and return what was read and the verdict."""
    wanted, first_run = audits(), first_run_audits()
    others = tuple(job for job in wanted if job not in first_run)
    record = RunRecord(started=clock())
    record.fleet_check = check_fleet()
    LOG.info("resetting the install's state")
    operations.reset(host_context, namespace)
    record.started = clock()
    since = datetime.fromtimestamp(record.started, timezone.utc).strftime(ISO)
    record.pods_before = pod_states(host_context, namespace)
    snap_start = _snapshot(install, record.errors)

    pending = stagger(others)
    marks: dict[str, tuple[float, int]] = {}

    def mark(job: str) -> None:
        attempts = marks.get(job, (0.0, 0))[1] + 1
        LOG.info("marking %s due (attempt %d)", job, attempts)
        record.errors += operations.trigger_jobs(host_context, namespace, PLATFORM_HOME, (job,))
        marks[job] = (clock(), attempts)

    while True:
        elapsed = clock() - record.started
        while pending and elapsed >= pending[0][0]:
            mark(pending.pop(0)[1])
        try:
            record.statuses = operations.job_statuses(host_context, namespace, PLATFORM_HOME, wanted, since)
        except kube.CommandError as exc:
            if NO_TABLE not in str(exc):
                LOG.warning("could not read the audits' runs: %s", exc)
        for job in to_remark(marks, record.statuses, clock()):
            mark(job)
        if not pending and burst_over(elapsed, record.statuses, wanted):
            break
        sleep(POLL_SECONDS)
    record.burst_seconds = clock() - record.started
    record.burst_statuses = dict(record.statuses)
    LOG.info("burst window over after %.0fs: %s", record.burst_seconds, record.statuses)
    soak_start = clock()
    snap_burst = _snapshot(install, record.errors)
    record.windows["burst"] = _window(install, record.started, soak_start, snap_start, snap_burst, record.errors)
    while clock() - soak_start < soak_seconds:
        sleep(POLL_SECONDS)
    snap_soak = _snapshot(install, record.errors)
    record.windows["soak"] = _window(install, soak_start, clock(), snap_burst, snap_soak, record.errors)

    # A read that fails here, a gateway pod missing or restarting, is the kind of failure the run
    # exists to catch: it is recorded, and the verdict is still written.
    try:
        record.statuses = operations.job_statuses(host_context, namespace, PLATFORM_HOME, wanted, since)
        record.job_seconds = operations.job_seconds(host_context, namespace, PLATFORM_HOME, wanted, since)
    except kube.CommandError as exc:
        record.errors.append(f"could not read the audits' runs at the end: {exc}")
    try:
        record.pods_after = pod_states(host_context, namespace)
    except kube.CommandError as exc:
        record.errors.append(f"could not read the install's pods at the end: {exc}")
        record.pods_after = {"(unread)": PodState(restarts=1)}
    try:
        record.sandbox_changed = log_lines(
            project, f'logName="projects/{project}/logs/events" AND jsonPayload.reason="SandboxChanged" '
            f'AND resource.labels.cluster_name="{host_cluster}" AND resource.labels.namespace_name="{namespace}"',
            record.started)
        record.node_oom_kills = log_lines(
            project, f'resource.type="k8s_node" AND resource.labels.cluster_name="{host_cluster}" '
            'AND textPayload:"OOMKilling"', record.started)
    except (kube.CommandError, subprocess.SubprocessError) as exc:
        record.errors.append(f"could not read Cloud Logging: {exc}")
    return record, verdict(record, wanted, first_run)
