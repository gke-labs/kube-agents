#!/usr/bin/env python3
"""workload_triage.py -- one read-only pass over a cluster's workload health.

The Cluster Agent's ``workload-triage-scan`` skill runs this once to answer
"what is failing?" or "is this cluster healthy?" without composing a command per
pod. It makes three ``kubectl get -o json`` reads, all namespaces at once
(falling back to per-namespace and batched reads if the credential proxy cuts an
all-namespaces reply at its output cap):

1. pods,
2. Deployments, StatefulSets, DaemonSets and Jobs,
3. Warning events.

It prints only what needs attention:

- unhealthy pods, with phase, reason, ready count, restarts, last exit code and
  the focused skill that owns the symptom;
- pods that are still starting, held by scheduling gates or terminating,
  reported under ``INITIALIZING`` and never waited on;
- pods that are Ready now but restarted before the events window, and pods
  that failed earlier and belong to a controller that replaces them (an
  evicted ReplicaSet pod) or to a Job that later completed, listed as history
  and not counted;
- controllers whose ready count is below the desired count;
- Warning events seen within the last 60 minutes, grouped by
  object and reason.

System namespaces (``kube-*``, ``gke-*``, ``gmp-*`` and ``*-system``) are folded
into one summary line each unless ``--include-system`` is given, so a restart in
a managed add-on does not pull the agent into exploring it. A system line names
its restarted pods with count and age, newest restart first.

The first line names the kubeconfig context and the UTC time of the scan, so
the output is its own audit trail. The script never mutates and never sleeps.
It always ends with ``unhealthy: <count>`` -- unhealthy pods plus controllers
below desired, outside system namespaces -- followed by
``system unhealthy: <count>`` (unhealthy pods plus controllers below desired in
folded system namespaces), both of which are ``0`` on a healthy cluster.

Usage::

    workload_triage.py
    workload_triage.py --include-system
    workload_triage.py --json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

KUBECTL = "kubectl"
PODS_RESOURCE = "pods"
CONTROLLERS_RESOURCE = "deployments,statefulsets,daemonsets,jobs"
EVENTS_RESOURCE = "events"
NAMESPACES_RESOURCE = "namespaces"
WARNING_EVENTS_FIELD_SELECTOR = ("--field-selector", "type=Warning")
NAMESPACE_PROJECTION_ARGS = ("-o", "custom-columns=NS:.metadata.namespace", "--no-headers")
PODS_ARGS = ("get", PODS_RESOURCE, "--all-namespaces", "-o", "json")
CONTROLLERS_ARGS = ("get", CONTROLLERS_RESOURCE, "--all-namespaces", "-o", "json")
EVENTS_ARGS = ("get", EVENTS_RESOURCE, "--all-namespaces", *WARNING_EVENTS_FIELD_SELECTOR, "-o", "json")
NAMESPACES_ARGS = ("get", NAMESPACES_RESOURCE, "-o", "name")

# In the Cluster Agent sandbox `kubectl` is the credential-proxy shim: when an
# all-namespaces reply passes CREDENTIAL_PROXY_MAX_OUTPUT_BYTES (8 MiB under the
# operator), the shim writes the prefix to stdout, prints this marker on stderr
# and returns kubectl's exit code 0.
PROXY_TRUNCATION_MARKER = "credential proxy output truncated"
OBJECT_BATCH_SIZE = 200
# Half of credential_proxy.DEFAULT_MAX_CONCURRENT_COMMANDS (8), so one cluster's
# truncation fallback leaves broker slots free for sibling Cluster Agents.
FALLBACK_CONCURRENCY = 4
# Same wall-clock budget pattern as cluster_preflight.sh: Hermes' terminal tool
# kills an unbudgeted call at TERMINAL_TOOL_TIMEOUT_SECONDS (180s) with no
# output, so the scan enforces its own budget inside that ceiling and exits 2
# cleanly when a fallback exceeds it.
TERMINAL_TOOL_TIMEOUT_SECONDS = 180
SCAN_BUDGET_MARGIN_SECONDS = 10
SCAN_BUDGET_DEFAULT_SECONDS = TERMINAL_TOOL_TIMEOUT_SECONDS - SCAN_BUDGET_MARGIN_SECONDS
BUDGET_ENV = "WORKLOAD_TRIAGE_BUDGET_SECONDS"
BROKER_ADMISSION_WAIT_SECONDS = 60
BROKER_KUBECTL_RUN_SECONDS = 60
KUBECTL_CAP_MARGIN_SECONDS = 5
KUBECTL_CAP_SECONDS = BROKER_ADMISSION_WAIT_SECONDS + BROKER_KUBECTL_RUN_SECONDS + KUBECTL_CAP_MARGIN_SECONDS
MAX_KUBECONFIG_BYTES = 1 << 20
DEFAULT_KUBECONFIG_RELPATH = (".kube", "config")
UNKNOWN_CONTEXT = "unknown"

EXIT_COMMAND_NOT_FOUND = 127
EXIT_TIMED_OUT = 124
EXIT_READ_FAILED = 2
ERROR_SNIPPET_CHARS = 200
ISO_TIMESTAMP_CHARS = 19
ISO_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"
SECONDS_PER_MINUTE = 60
MINUTES_PER_HOUR = 60
HOURS_DISPLAY_LIMIT_MINUTES = 48 * MINUTES_PER_HOUR
MINUTES_PER_DAY = 24 * MINUTES_PER_HOUR

# Namespaces a cluster-wide scan summarises instead of listing: Kubernetes' own,
# GKE's managed add-ons, Managed Prometheus, and the `<operator>-system`
# convention most controllers install under.
SYSTEM_NAMESPACES = frozenset({"kube-system", "kube-public", "kube-node-lease"})
SYSTEM_NAMESPACE_PREFIXES = ("kube-", "gke-", "gmp-")
SYSTEM_NAMESPACE_SUFFIXES = ("-system",)

# A pod younger than this that is still pulling, mounting or scheduling is
# starting, not stuck. Ten minutes matches a Deployment's default
# progressDeadlineSeconds and covers an Autopilot node scale-up.
DEFAULT_INIT_GRACE_MINUTES = 10
DEFAULT_EVENTS_MINUTES = 60
EVENT_MESSAGE_CHARS = 140
# How many restarted pods a system namespace's summary line names, newest
# restart first; the rest are counted.
SYSTEM_RESTARTS_NAMED = 5
TOP_NODE_REASONS = 5
UNKNOWN_RESTART_MINUTES = 1 << 30

# The focused skill each symptom hands off to.
SKILL_CRASHLOOP = "pod-crashloop-oom"
SKILL_PENDING = "pod-pending-scheduling"
SKILL_CREATING = "pod-container-creating"

CRASH_WAITING_REASONS = frozenset({"CrashLoopBackOff", "RunContainerError"})
IMAGE_WAITING_REASONS = frozenset({"ImagePullBackOff", "ErrImagePull", "InvalidImageName", "ErrImageNeverPull"})
CONFIG_WAITING_REASONS = frozenset({"CreateContainerConfigError", "CreateContainerError"})
STARTING_WAITING_REASONS = frozenset({"ContainerCreating", "PodInitializing"})
SCHEDULING_GATED_REASON = "SchedulingGated"
TERMINATING_REASON = "Terminating"
UNSCHEDULABLE_REASON = "Unschedulable"
NOT_READY_REASON = "NotReady"
RECENT_RESTART_REASON = "RecentRestart"
RESTARTED_EARLIER_REASON = "RestartedEarlier"

STATUS_INITIALIZING = "initializing"

# What a pod row is for. Only CATEGORY_UNHEALTHY rows count toward `unhealthy:`
# and name a skill; the others are context the answer quotes without digging.
CATEGORY_UNHEALTHY = "unhealthy"
CATEGORY_INITIALIZING = "initializing"
# Running and Ready now, restarted before the events window: the restart count
# a "find the restarting pods" question asks for, not a live failure.
CATEGORY_RESTARTED_EARLIER = "restarted_earlier"
# Failed (an eviction, say) before the events window and owned by a controller
# that replaces its pods, or a retained Failed attempt pod of a Job that later
# completed. The pod object lingers until garbage collection; a shortfall in its
# replacement shows in CONTROLLERS BELOW DESIRED instead.
CATEGORY_STALE_FAILED = "stale_failed"
REPLACING_OWNER_KINDS = frozenset({"ReplicaSet", "StatefulSet", "DaemonSet"})
JOB_KIND = "Job"
DAEMONSET_KIND = "DaemonSet"
CONTROLLER_KINDS = frozenset({"Deployment", "StatefulSet", DAEMONSET_KIND, JOB_KIND})

EVENT_STATE_UNHEALTHY = "unhealthy"
EVENT_STATE_INITIALIZING = "initializing"
EVENT_STATE_HEALTHY = "healthy"
EVENT_STATE_STALE_FAILED = "stale_failed"
EVENT_STATE_GONE = "gone"
EVENT_STATE_OTHER = "-"

PHASE_SUCCEEDED = "Succeeded"
PHASE_FAILED = "Failed"
PHASE_PENDING = "Pending"
CONDITION_TRUE = "True"
CONDITION_FALSE = "False"
CONDITION_COMPLETE = "Complete"
CONDITION_FAILED = "Failed"
CONDITION_READY = "Ready"
CONDITION_POD_SCHEDULED = "PodScheduled"


def scan_budget_seconds(env: Mapping[str, str] | None = None) -> float:
    """Total wall-clock budget for one scan, overridable via WORKLOAD_TRIAGE_BUDGET_SECONDS."""
    environ = os.environ if env is None else env
    raw = environ.get(BUDGET_ENV, "").strip()
    if raw:
        try:
            parsed = float(raw)
        except ValueError:
            parsed = 0.0
        if math.isfinite(parsed) and parsed > 0:
            return parsed
    return float(SCAN_BUDGET_DEFAULT_SECONDS)


def run_kubectl(args: tuple[str, ...], *, timeout: float | None = None) -> tuple[int, str, str]:
    """Run one kubectl read; a missing binary or expired timeout is a failed read, not a crash."""
    try:
        res = subprocess.run(
            [KUBECTL, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        shown = f"{timeout:.1f}s" if timeout is not None else "budget"
        return EXIT_TIMED_OUT, "", f"kubectl timed out after {shown}"
    except OSError as exc:
        return EXIT_COMMAND_NOT_FOUND, "", str(exc)
    return res.returncode, res.stdout, res.stderr


def _bounded_runner(
    run,
    clock: Callable[[], float],
    deadline: float,
) -> Callable[[tuple[str, ...]], tuple[int, str, str]]:
    """Wrap `run` so no new kubectl call starts after `deadline` and subprocess calls carry a timeout."""

    def call(args: tuple[str, ...]) -> tuple[int, str, str]:
        remaining = deadline - clock()
        if remaining <= 0:
            return EXIT_TIMED_OUT, "", "workload triage scan budget exceeded"
        if run is run_kubectl:
            return run_kubectl(args, timeout=min(float(KUBECTL_CAP_SECONDS), remaining))
        return run(args)

    return call


def _parse_current_context(text: str) -> str | None:
    """Extract `current-context` from kubeconfig YAML text without invoking kubectl."""
    try:
        from credential_proxy_client import read_current_context

        return read_current_context(text)
    except ImportError:
        try:
            import yaml
        except ImportError:
            return None
        try:
            document = yaml.safe_load(text)
        except (yaml.YAMLError, RecursionError):
            return None
        if not isinstance(document, dict):
            return None
        context = document.get("current-context")
        if not isinstance(context, str):
            return None
        return context.strip() or None
    except (ValueError, LookupError, AttributeError):
        return None


def _read_kubeconfig_context(path: Path) -> str | None:
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_KUBECONFIG_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_KUBECONFIG_BYTES:
        return None
    return _parse_current_context(raw.decode("utf-8", errors="replace"))


def current_context(env: Mapping[str, str] | None = None, home: Path | None = None) -> str:
    """The kubeconfig context the reads used, read locally from disk (never via
    `kubectl config current-context`, which queues a broker exec in the sandbox)."""
    environ = os.environ if env is None else env
    raw_kubeconfig = environ.get("KUBECONFIG", "").strip()
    if raw_kubeconfig:
        for entry in raw_kubeconfig.split(os.pathsep):
            entry = entry.strip()
            if not entry:
                continue
            ctx = _read_kubeconfig_context(Path(entry))
            if ctx:
                return ctx
        return UNKNOWN_CONTEXT
    if env is None and home is None:
        try:
            from credential_proxy_client import shell_context

            pinned = shell_context()
            if pinned:
                return pinned
        except ImportError:
            pass
    default_path = (home or Path.home()).joinpath(*DEFAULT_KUBECONFIG_RELPATH)
    return _read_kubeconfig_context(default_path) or UNKNOWN_CONTEXT


def _parse_json_reply(
    code: int,
    out: str,
    err: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, list[dict] | None, str]:
    """Classify one `-o json` kubectl reply as ('ok', items, ''), ('truncated', None, reason),
    or ('failed', None, reason)."""
    if PROXY_TRUNCATION_MARKER in err:
        return "truncated", None, PROXY_TRUNCATION_MARKER
    if code != 0:
        return "failed", None, (err or out).strip()[:ERROR_SNIPPET_CHARS]
    if allow_empty and not out.strip():
        return "ok", [], ""
    try:
        parsed = json.loads(out)
    except ValueError:
        return "truncated", None, "cannot parse output as JSON"
    if not isinstance(parsed, dict):
        return "truncated", None, "cannot parse output as JSON"
    items = (parsed.get("items") or []) if "items" in parsed else [parsed]
    return "ok", items, ""


def _list_namespaces(run, cache: list[list[str] | None]) -> tuple[list[str] | None, str]:
    if cache[0] is not None:
        return cache[0], ""
    code, out, err = run(NAMESPACES_ARGS)
    if PROXY_TRUNCATION_MARKER in err:
        return None, PROXY_TRUNCATION_MARKER
    if code != 0:
        return None, (err or out).strip()[:ERROR_SNIPPET_CHARS]
    namespaces = [
        line.partition("/")[2] if "/" in line else line
        for line in (raw.strip() for raw in out.splitlines())
        if line
    ]
    if not namespaces:
        return None, "no namespaces returned"
    cache[0] = namespaces
    return namespaces, ""


def _populated_namespaces(
    run,
    resource: str,
    extra_args: tuple[str, ...],
    cache: list[list[str] | None],
) -> tuple[list[str] | None, str]:
    """Find only the namespaces that actually hold `resource` in one compact read,
    falling back to `kubectl get namespaces -o name` only if that projection truncates."""
    code, out, err = run(("get", resource, "--all-namespaces", *extra_args, *NAMESPACE_PROJECTION_ARGS))
    if PROXY_TRUNCATION_MARKER in err:
        return _list_namespaces(run, cache)
    if code != 0:
        return None, (err or out).strip()[:ERROR_SNIPPET_CHARS]
    populated = list(
        dict.fromkeys(
            ns for ns in (raw.strip() for raw in out.splitlines()) if ns and ns != "<none>"
        )
    )
    return populated, ""


def _read_namespace(
    run,
    resource: str,
    extra_args: tuple[str, ...],
    what: str,
    ns: str,
) -> tuple[list[dict] | None, str]:
    ns_code, ns_out, ns_err = run(("get", resource, "-n", ns, *extra_args, "-o", "json"))
    ns_state, ns_items, ns_detail = _parse_json_reply(ns_code, ns_out, ns_err)
    if ns_state == "ok":
        return ns_items or [], ""
    if ns_state == "failed":
        return None, f"cannot read {what} in {ns}: {ns_detail}"
    name_code, name_out, name_err = run(("get", resource, "-n", ns, *extra_args, "-o", "name"))
    if PROXY_TRUNCATION_MARKER in name_err or name_code != 0:
        detail = (
            PROXY_TRUNCATION_MARKER
            if PROXY_TRUNCATION_MARKER in name_err
            else (name_err or name_out).strip()[:ERROR_SNIPPET_CHARS]
        )
        return None, f"cannot read {what} in {ns}: {detail}"
    names = [line.strip() for line in name_out.splitlines() if line.strip()]
    ns_collected: list[dict] = []
    batch_size = OBJECT_BATCH_SIZE
    cursor = 0
    retry_queue: list[list[str]] = []
    while retry_queue or cursor < len(names):
        if retry_queue:
            batch = retry_queue.pop(0)
        else:
            batch = names[cursor : cursor + batch_size]
            cursor += len(batch)
        b_code, b_out, b_err = run(("get", "--ignore-not-found", *batch, "-n", ns, "-o", "json"))
        b_state, b_items, b_detail = _parse_json_reply(b_code, b_out, b_err, allow_empty=True)
        if b_state == "ok":
            ns_collected.extend(b_items or [])
            continue
        if b_state == "truncated" and len(batch) > 1:
            mid = len(batch) // 2
            batch_size = min(batch_size, mid)
            retry_queue = [batch[:mid], batch[mid:], *retry_queue]
            continue
        return None, f"cannot read {what} in {ns}: {b_detail}"
    return ns_collected, ""


def read_resource(
    run,
    resource: str,
    extra_args: tuple[str, ...],
    what: str,
    errors: list[str],
    namespaces_cache: list[list[str] | None],
) -> list[dict] | None:
    """Read `resource` across all namespaces, falling back to bounded concurrent
    per-namespace and per-object batched reads when the credential proxy truncates
    an over-cap reply.

    Returns the collected object list, or `None` after appending an actionable
    error to `errors` when a read fails or truncation cannot be recovered.
    """
    code, out, err = run(("get", resource, "--all-namespaces", *extra_args, "-o", "json"))
    state, items, reason = _parse_json_reply(code, out, err)
    if state == "ok":
        return items
    if state == "failed":
        errors.append(f"cannot read {what}: {reason}")
        return None

    namespaces, ns_reason = _populated_namespaces(run, resource, extra_args, namespaces_cache)
    if namespaces is None:
        errors.append(f"cannot read {what}: {reason} (namespace fallback failed: {ns_reason})")
        return None
    if not namespaces:
        return []
    if len(namespaces) == 1:
        ns_items, ns_err = _read_namespace(run, resource, extra_args, what, namespaces[0])
        if ns_items is None:
            errors.append(ns_err)
            return None
        return ns_items

    cancel_event = threading.Event()
    first_error: list[str] = []
    error_lock = threading.Lock()

    def cancellable_run(args: tuple[str, ...]) -> tuple[int, str, str]:
        if cancel_event.is_set():
            return 1, "", ""
        return run(args)

    def read_one(ns: str) -> list[dict] | None:
        if cancel_event.is_set():
            return None
        ns_items, ns_err = _read_namespace(cancellable_run, resource, extra_args, what, ns)
        if ns_items is None:
            with error_lock:
                if not first_error and ns_err:
                    first_error.append(ns_err)
                cancel_event.set()
        return ns_items

    results_by_idx: dict[int, list[dict]] = {}
    workers = min(FALLBACK_CONCURRENCY, len(namespaces))
    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        future_to_idx = {
            executor.submit(read_one, ns): idx
            for idx, ns in enumerate(namespaces)
        }
        for fut in as_completed(future_to_idx):
            ns_items = fut.result()
            if ns_items is None:
                cancel_event.set()
                with error_lock:
                    errors.append(first_error[0])
                executor.shutdown(wait=True, cancel_futures=True)
                return None
            results_by_idx[future_to_idx[fut]] = ns_items
    finally:
        cancel_event.set()
        executor.shutdown(wait=True, cancel_futures=True)
    collected: list[dict] = []
    for idx in range(len(namespaces)):
        collected.extend(results_by_idx[idx])
    return collected


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:ISO_TIMESTAMP_CHARS], ISO_TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def format_age(start: datetime | None, now: datetime) -> str:
    if start is None:
        return "?"
    minutes = int((now - start).total_seconds() // SECONDS_PER_MINUTE)
    if minutes < MINUTES_PER_HOUR:
        return f"{max(minutes, 0)}m"
    if minutes < HOURS_DISPLAY_LIMIT_MINUTES:
        return f"{minutes // MINUTES_PER_HOUR}h"
    return f"{minutes // MINUTES_PER_DAY}d"


def is_system_namespace(name: str) -> bool:
    return (
        name in SYSTEM_NAMESPACES
        or name.startswith(SYSTEM_NAMESPACE_PREFIXES)
        or name.endswith(SYSTEM_NAMESPACE_SUFFIXES)
    )


def _condition(pod: dict, ctype: str) -> dict:
    for cond in pod.get("status", {}).get("conditions") or []:
        if cond.get("type") == ctype:
            return cond
    return {}


def _terminations(init_statuses: list[dict], statuses: list[dict]) -> list[tuple[datetime | None, str]]:
    """Every recorded container termination as (finishedAt, `<code> <reason>`).

    An init container that exited 0 did its job, so its termination is not a
    failure and is left out: otherwise every pod with an init container would
    show `0 Completed` as its last exit.
    """
    out = []
    for is_init, group in ((True, init_statuses), (False, statuses)):
        for cs in group:
            for state_key in ("state", "lastState"):
                term = (cs.get(state_key) or {}).get("terminated")
                if not term or (is_init and term.get("exitCode") == 0):
                    continue
                label = f"{term.get('exitCode', '?')} {term.get('reason') or ''}".strip()
                out.append((parse_time(term.get("finishedAt")), label))
    return out


def _last_exit(terminations: list[tuple[datetime | None, str]]) -> str:
    """The label of the most recent termination, or `-`."""
    dated = [t for t in terminations if t[0] is not None]
    if dated:
        return max(dated, key=lambda t: t[0])[1]
    return terminations[-1][1] if terminations else "-"


def _job_completed(obj: dict) -> bool:
    """Whether a Job finished successfully, superseding any earlier Failed attempt pods."""
    if obj.get("kind") != JOB_KIND:
        return False
    status = obj.get("status") or {}
    for cond in status.get("conditions") or []:
        if cond.get("type") == CONDITION_FAILED and cond.get("status") == CONDITION_TRUE:
            return False
        if cond.get("type") == CONDITION_COMPLETE and cond.get("status") == CONDITION_TRUE:
            return True
    completions = (obj.get("spec") or {}).get("completions")
    target = 1 if completions is None else int(completions)
    return target > 0 and int(status.get("succeeded") or 0) >= target


def classify_pod(
    pod: dict,
    now: datetime,
    grace: timedelta,
    window: timedelta,
    completed_jobs: frozenset[tuple[str, str]] | set[tuple[str, str]] = frozenset(),
) -> dict | None:
    """One row for a pod that needs attention, or None for a healthy or finished pod.

    The row's ``category`` says what it is: ``unhealthy`` rows name the ``skill``
    that owns their symptom; ``initializing`` rows are pods still starting inside
    the grace period, held by scheduling gates or terminating; ``restarted_earlier``
    and ``stale_failed`` rows are history, reported but not counted.
    """
    meta = pod.get("metadata", {})
    status = pod.get("status", {})
    phase = status.get("phase") or "Unknown"
    if phase == PHASE_SUCCEEDED:
        return None
    created = parse_time(meta.get("creationTimestamp"))
    young = created is not None and now - created < grace
    init_statuses = status.get("initContainerStatuses") or []
    statuses = status.get("containerStatuses") or []
    all_statuses = init_statuses + statuses
    restarts = sum(int(cs.get("restartCount") or 0) for cs in all_statuses)
    terminations = _terminations(init_statuses, statuses)
    ready_count = sum(1 for cs in statuses if cs.get("ready"))
    total = len(pod.get("spec", {}).get("containers") or statuses)
    namespace = meta.get("namespace", "")
    row = {
        "namespace": namespace,
        "pod": meta.get("name", ""),
        "phase": phase,
        "ready": f"{ready_count}/{total}",
        "restarts": restarts,
        "last_exit": _last_exit(terminations),
        "age": format_age(created, now),
    }

    def verdict(reason: str, skill: str | None, category: str | None = None) -> dict:
        row["category"] = category or (CATEGORY_UNHEALTHY if skill else CATEGORY_INITIALIZING)
        row["status"] = STATUS_INITIALIZING if row["category"] == CATEGORY_INITIALIZING else reason
        row["reason"] = reason
        row["skill"] = skill if row["category"] == CATEGORY_UNHEALTHY else ""
        return row

    last_finished = max((t[0] for t in terminations if t[0] is not None), default=None)
    if restarts:
        row["last_restart"] = format_age(last_finished, now) + " ago" if last_finished else "?"
        row["last_restart_minutes"] = (
            int((now - last_finished).total_seconds() // SECONDS_PER_MINUTE) if last_finished else None
        )

    # Not-a-symptom states shared with upgrade_retrospective.py: a pod being
    # deleted (`metadata.deletionTimestamp`) or held by `spec.schedulingGates`
    # (`SchedulingGated`) is departing or waiting for queue admission regardless
    # of age, never a failing workload.
    if meta.get("deletionTimestamp"):
        return verdict(TERMINATING_REASON, None, CATEGORY_INITIALIZING)
    scheduled = _condition(pod, CONDITION_POD_SCHEDULED)
    if phase == PHASE_PENDING and (
        (scheduled.get("status") == CONDITION_FALSE and scheduled.get("reason") == SCHEDULING_GATED_REASON)
        or bool(pod.get("spec", {}).get("schedulingGates"))
    ):
        return verdict(SCHEDULING_GATED_REASON, None, CATEGORY_INITIALIZING)

    if phase == PHASE_FAILED:
        # When the pod failed: its last container exit, else the newest
        # condition change (an eviction often records no finishedAt), and only
        # then its start.
        transitions = [parse_time(c.get("lastTransitionTime")) for c in status.get("conditions") or []]
        ended = (
            last_finished
            or max((t for t in transitions if t is not None), default=None)
            or parse_time(status.get("startTime"))
            or created
        )
        stale = ended is not None and now - ended > window
        # History when a controller replaces the pod (after the events window)
        # or when its owning Job later completed. An incomplete/failed Job's pod
        # or a bare pod stays a failure however old it is.
        owner_refs = meta.get("ownerReferences") or []
        owners = {o.get("kind") for o in owner_refs}
        replaced = stale and bool(owners & REPLACING_OWNER_KINDS)
        superseded_job = any(
            o.get("kind") == JOB_KIND and (namespace, o.get("name", "")) in completed_jobs
            for o in owner_refs
        )
        category = CATEGORY_STALE_FAILED if (replaced or superseded_job) else None
        return verdict(status.get("reason") or PHASE_FAILED, SKILL_CRASHLOOP, category)

    waiting = [((cs.get("state") or {}).get("waiting") or {}).get("reason") or "" for cs in all_statuses]
    for reason in waiting:
        if reason in CRASH_WAITING_REASONS:
            return verdict(reason, SKILL_CRASHLOOP)
    for reason in waiting:
        if reason in IMAGE_WAITING_REASONS or reason in CONFIG_WAITING_REASONS:
            return verdict(reason, SKILL_CREATING)

    if phase == PHASE_PENDING and scheduled.get("status") == CONDITION_FALSE:
        reason = scheduled.get("reason") or UNSCHEDULABLE_REASON
        # Unschedulable while a node is being added is still starting.
        return verdict(reason, None if young else SKILL_PENDING)

    for reason in waiting:
        if reason in STARTING_WAITING_REASONS:
            return verdict(reason, None if young else SKILL_CREATING)

    if phase == PHASE_PENDING:
        return verdict(PHASE_PENDING, None if young else SKILL_PENDING)

    if _condition(pod, CONDITION_READY).get("status") != CONDITION_TRUE:
        return verdict(NOT_READY_REASON, None if young else SKILL_CRASHLOOP)

    # Running and Ready, but restarted inside the events window: an intermittent crash.
    if restarts and last_finished is not None and now - last_finished < window:
        return verdict(RECENT_RESTART_REASON, SKILL_CRASHLOOP)
    if restarts:
        return verdict(RESTARTED_EARLIER_REASON, SKILL_CRASHLOOP, CATEGORY_RESTARTED_EARLIER)
    return None


def _job_failed_reason(obj: dict) -> str | None:
    """The terminal `Failed` condition's reason on an inactive, incomplete Job, or None."""
    if obj.get("kind") != JOB_KIND or _job_completed(obj):
        return None
    status = obj.get("status") or {}
    if status.get("active"):
        return None
    for cond in status.get("conditions") or []:
        if cond.get("type") == CONDITION_FAILED and cond.get("status") == CONDITION_TRUE:
            return str(cond.get("reason") or CONDITION_FAILED).strip() or CONDITION_FAILED
    return None


def classify_controller(obj: dict) -> dict | None:
    """A row for a controller below its desired ready count, or a Job whose Failed condition is True."""
    kind = obj.get("kind", "")
    spec = obj.get("spec", {})
    status = obj.get("status", {})
    meta = obj.get("metadata", {})
    if kind == JOB_KIND:
        failed_reason = _job_failed_reason(obj)
        if failed_reason is None:
            return None
        completions = spec.get("completions")
        desired = max(1 if completions is None else int(completions), 1)
        ready = int(status.get("succeeded") or 0)
        return {
            "namespace": meta.get("namespace", ""),
            "controller": f"{JOB_KIND}/{meta.get('name', '')}",
            "desired": desired,
            "ready": ready,
            "updated": 0,
            "reason": failed_reason,
        }
    if kind == DAEMONSET_KIND:
        desired = int(status.get("desiredNumberScheduled") or 0)
        ready = int(status.get("numberReady") or 0)
        updated = int(status.get("updatedNumberScheduled") or 0)
    else:
        replicas = spec.get("replicas")
        desired = 1 if replicas is None else int(replicas)
        ready = int(status.get("readyReplicas") or 0)
        updated = int(status.get("updatedReplicas") or 0)
    if desired == 0 or ready >= desired:
        return None
    return {
        "namespace": meta.get("namespace", ""),
        "controller": f"{kind}/{meta.get('name', '')}",
        "desired": desired,
        "ready": ready,
        "updated": updated,
    }


def event_time(ev: dict) -> datetime | None:
    return (
        parse_time((ev.get("series") or {}).get("lastObservedTime"))
        or parse_time(ev.get("lastTimestamp"))
        or parse_time(ev.get("eventTime"))
        or parse_time(ev.get("metadata", {}).get("creationTimestamp"))
    )


def group_events(events: list[dict], now: datetime, window: timedelta) -> list[dict]:
    """Warning events inside the window, one row per (namespace, object, reason), newest first."""
    groups: dict[tuple[str, str, str], dict] = {}
    for ev in events:
        seen = event_time(ev)
        if seen is None or now - seen > window:
            continue
        inv = ev.get("involvedObject") or ev.get("regarding") or {}
        namespace = inv.get("namespace") or ev.get("metadata", {}).get("namespace", "")
        obj = f"{inv.get('kind', '')}/{inv.get('name', '')}"
        reason = ev.get("reason") or ""
        count = int((ev.get("series") or {}).get("count") or ev.get("count") or 1)
        key = (namespace, obj, reason)
        row = groups.get(key)
        message = " ".join((ev.get("message") or ev.get("note") or "").split())[:EVENT_MESSAGE_CHARS]
        if row is None:
            groups[key] = {
                "namespace": namespace,
                "object": obj,
                "reason": reason,
                "count": count,
                "last_seen": seen,
                "message": message,
            }
        else:
            row["count"] += count
            if seen > row["last_seen"]:
                row["last_seen"] = seen
                row["message"] = message
    rows = sorted(groups.values(), key=lambda r: r["last_seen"], reverse=True)
    for row in rows:
        row["last_seen"] = format_age(row["last_seen"], now) + " ago"
    return rows


def triage(
    run,
    now: datetime,
    include_system: bool,
    grace_minutes: int,
    events_minutes: int,
    *,
    context: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    budget_seconds: float | None = None,
) -> dict:
    errors: list[str] = []
    budget = scan_budget_seconds() if budget_seconds is None else budget_seconds
    bounded_run = _bounded_runner(run, clock, clock() + budget)
    grace = timedelta(minutes=grace_minutes)
    window = timedelta(minutes=events_minutes)
    namespaces_cache: list[list[str] | None] = [None]
    pods = read_resource(bounded_run, PODS_RESOURCE, (), "pods", errors, namespaces_cache)
    if pods is None:
        return {"errors": errors}
    controllers = read_resource(
        bounded_run,
        CONTROLLERS_RESOURCE,
        (),
        "deployments, statefulsets, daemonsets and jobs",
        errors,
        namespaces_cache,
    )
    if controllers is None:
        return {"errors": errors}
    events = read_resource(
        bounded_run,
        EVENTS_RESOURCE,
        WARNING_EVENTS_FIELD_SELECTOR,
        "warning events",
        errors,
        namespaces_cache,
    )
    if events is None:
        return {"errors": errors}

    completed_jobs = {
        (obj.get("metadata", {}).get("namespace", ""), obj.get("metadata", {}).get("name", ""))
        for obj in controllers
        if _job_completed(obj)
    }

    def shown(namespace: str) -> bool:
        return include_system or not is_system_namespace(namespace)

    system: dict[str, dict] = {}

    def system_entry(namespace: str) -> dict:
        return system.setdefault(
            namespace,
            {"pods": 0, "unhealthy_pods": 0, "reasons": {}, "controllers_short": 0, "warnings": 0, "restarted": []},
        )

    def has_findings(e: dict) -> bool:
        return bool(e["unhealthy_pods"] or e["controllers_short"] or e["warnings"] or e["restarted"])

    rows: dict[str, list[dict]] = {
        CATEGORY_UNHEALTHY: [],
        CATEGORY_INITIALIZING: [],
        CATEGORY_RESTARTED_EARLIER: [],
        CATEGORY_STALE_FAILED: [],
    }
    pod_states: dict[tuple[str, str], str] = {}
    counted = 0
    for pod in pods:
        meta = pod.get("metadata", {})
        namespace = meta.get("namespace", "")
        pod_name = meta.get("name", "")
        if (pod.get("status") or {}).get("phase") == PHASE_SUCCEEDED:
            pod_states[(namespace, pod_name)] = EVENT_STATE_HEALTHY
            continue
        row = classify_pod(pod, now, grace, window, completed_jobs=completed_jobs)
        if row is None or row["category"] == CATEGORY_RESTARTED_EARLIER:
            pod_states[(namespace, pod_name)] = EVENT_STATE_HEALTHY
        elif row["category"] == CATEGORY_INITIALIZING:
            pod_states[(namespace, pod_name)] = EVENT_STATE_INITIALIZING
        elif row["category"] == CATEGORY_STALE_FAILED:
            pod_states[(namespace, pod_name)] = EVENT_STATE_STALE_FAILED
        else:
            pod_states[(namespace, pod_name)] = EVENT_STATE_UNHEALTHY
        if not shown(namespace):
            entry = system_entry(namespace)
            entry["pods"] += 1
            if row and row["category"] == CATEGORY_UNHEALTHY:
                entry["unhealthy_pods"] += 1
                entry["reasons"][row["reason"]] = entry["reasons"].get(row["reason"], 0) + 1
            # Restart counts answer "which pods restarted?" without listing
            # the namespace, so they ride on the summary line.
            if row and row["restarts"]:
                minutes = row.get("last_restart_minutes")
                entry["restarted"].append(
                    {
                        "pod": row["pod"],
                        "restarts": row["restarts"],
                        "last_restart": row.get("last_restart", "?"),
                        "minutes": minutes,
                        "recent": minutes is not None and minutes < events_minutes,
                    }
                )
            continue
        counted += 1
        if row is None:
            continue
        rows[row["category"]].append(row)
    unhealthy, initializing = rows[CATEGORY_UNHEALTHY], rows[CATEGORY_INITIALIZING]
    restarted_earlier, stale_failed = rows[CATEGORY_RESTARTED_EARLIER], rows[CATEGORY_STALE_FAILED]
    restarted_earlier.sort(key=lambda r: -r["restarts"])

    short = []
    controller_states: dict[tuple[str, str], str] = {}
    for obj in controllers:
        c_meta = obj.get("metadata", {})
        c_ns = c_meta.get("namespace", "")
        c_ref = f"{obj.get('kind', '')}/{c_meta.get('name', '')}"
        row = classify_controller(obj)
        controller_states[(c_ns, c_ref)] = EVENT_STATE_HEALTHY if row is None else EVENT_STATE_UNHEALTHY
        if row is None:
            continue
        if shown(row["namespace"]):
            short.append(row)
        else:
            system_entry(row["namespace"])["controllers_short"] += 1

    warnings = []
    node_events = {"count": 0, "nodes": set(), "reasons": {}}
    for row in group_events(events, now, window):
        if row["object"].startswith("Node/") and not include_system:
            node_events["count"] += row["count"]
            node_events["nodes"].add(row["object"])
            node_events["reasons"][row["reason"]] = node_events["reasons"].get(row["reason"], 0) + row["count"]
        elif shown(row["namespace"]):
            kind, _, name = row["object"].partition("/")
            if kind == "Pod":
                row["state"] = pod_states.get((row["namespace"], name), EVENT_STATE_GONE)
            elif kind in CONTROLLER_KINDS:
                row["state"] = controller_states.get((row["namespace"], row["object"]), EVENT_STATE_GONE)
            else:
                row["state"] = EVENT_STATE_OTHER
            warnings.append(row)
        else:
            system_entry(row["namespace"])["warnings"] += row["count"]

    system_unhealthy = sum(e["unhealthy_pods"] + e["controllers_short"] for e in system.values())
    return {
        "scanned_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "context": current_context() if context is None else context,
        "summary": {
            "pods": counted,
            "healthy": counted - len(unhealthy) - len(initializing) - len(stale_failed),
            "initializing": len(initializing),
            "unhealthy_pods": len(unhealthy),
            "restarted_earlier": len(restarted_earlier),
            "stale_failed": len(stale_failed),
            "controllers_below_desired": len(short),
            "warning_events": sum(r["count"] for r in warnings),
            "events_minutes": events_minutes,
            "unhealthy": len(unhealthy) + len(short),
            "system_unhealthy": system_unhealthy,
        },
        "unhealthy_pods": unhealthy,
        "initializing": initializing,
        "restarted_earlier": restarted_earlier,
        "stale_failed": stale_failed,
        "controllers_below_desired": short,
        "warning_events": warnings,
        "system_namespaces": {ns: e for ns, e in sorted(system.items()) if has_findings(e)},
        "node_warning_events": {
            "count": node_events["count"],
            "nodes": len(node_events["nodes"]),
            "reasons": dict(sorted(node_events["reasons"].items(), key=lambda kv: -kv[1])),
        },
        "system_namespaces_healthy": sorted(ns for ns, e in system.items() if not has_findings(e)),
        "errors": errors,
    }


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip()]
    for row in rows:
        lines.append("  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip())
    return lines


def render_text(report: dict) -> str:
    s = report["summary"]
    out = [
        f"scanned {report['scanned_at']} context {report['context']}",
        f"pods: {s['pods']} (healthy {s['healthy']}, initializing {s['initializing']}, unhealthy {s['unhealthy_pods']}"
        f"{', failed earlier ' + str(s['stale_failed']) if s['stale_failed'] else ''})"
        f"  controllers below desired: {s['controllers_below_desired']}"
        f"  warning events ({s['events_minutes']}m): {s['warning_events']}",
    ]

    def section(title: str, headers: list[str], rows: list[list[str]]) -> None:
        if not rows:
            return
        out.append("")
        out.append(title)
        out.extend(_table(headers, rows))

    section(
        "UNHEALTHY PODS",
        ["NAMESPACE", "POD", "STATUS", "READY", "RESTARTS", "LAST EXIT", "AGE", "SKILL"],
        [[r["namespace"], r["pod"], r["status"], r["ready"], str(r["restarts"]), r["last_exit"], r["age"], r["skill"]] for r in report["unhealthy_pods"]],
    )
    section(
        "INITIALIZING (starting, gated or terminating; report as initializing, do not wait)",
        ["NAMESPACE", "POD", "REASON", "AGE"],
        [[r["namespace"], r["pod"], r["reason"], r["age"]] for r in report["initializing"]],
    )
    section(
        "RESTARTED EARLIER (Ready now; last restart before the events window; not counted)",
        ["NAMESPACE", "POD", "RESTARTS", "LAST EXIT", "LAST RESTART"],
        [[r["namespace"], r["pod"], str(r["restarts"]), r["last_exit"], r["last_restart"]] for r in report["restarted_earlier"]],
    )
    section(
        "FAILED EARLIER (replaced by their controller or superseded by a completed Job; not counted)",
        ["NAMESPACE", "POD", "REASON", "LAST EXIT", "AGE"],
        [[r["namespace"], r["pod"], r["reason"], r["last_exit"], r["age"]] for r in report["stale_failed"]],
    )
    section(
        "CONTROLLERS BELOW DESIRED",
        ["NAMESPACE", "CONTROLLER", "DESIRED", "READY", "UPDATED"],
        [
            [
                r["namespace"],
                r["controller"],
                str(r["desired"]),
                str(r["ready"]),
                f"failed: {r['reason']}" if r.get("reason") else str(r["updated"]),
            ]
            for r in report["controllers_below_desired"]
        ],
    )
    section(
        "WARNING EVENTS",
        ["NAMESPACE", "OBJECT", "STATE", "REASON", "COUNT", "LAST SEEN", "MESSAGE"],
        [
            [r["namespace"], r["object"], r["state"], r["reason"], str(r["count"]), r["last_seen"], r["message"]]
            for r in report["warning_events"]
        ],
    )
    system = report["system_namespaces"]
    nodes = report["node_warning_events"]
    if system or report["system_namespaces_healthy"] or nodes["count"]:
        out.append("")
        out.append("SYSTEM NAMESPACES AND NODES (summarised; --include-system lists them)")
        if nodes["count"]:
            top = ", ".join(f"{k} {v}" for k, v in list(nodes["reasons"].items())[:TOP_NODE_REASONS])
            out.append(f"nodes: {nodes['count']} warning events on {nodes['nodes']} nodes ({top})")
        for ns, e in system.items():
            reasons = ", ".join(f"{k} {v}" for k, v in sorted(e["reasons"].items()))
            out.append(
                f"{ns}: {e['unhealthy_pods']}/{e['pods']} pods unhealthy"
                + (f" ({reasons})" if reasons else "")
                + f", {e['controllers_short']} controllers below desired, {e['warnings']} warning events"
                + _restarted_clause(e["restarted"])
            )
        if report["system_namespaces_healthy"]:
            out.append(f"no findings: {', '.join(report['system_namespaces_healthy'])}")
    out.append("")
    out.append(f"unhealthy: {s['unhealthy']}")
    out.append(f"system unhealthy: {s['system_unhealthy']}")
    return "\n".join(out)


def _restarted_clause(restarted: list[dict]) -> str:
    """Restarted system pods, newest restart first, so one that restarted in the
    events window is never crowded out by pods that restarted at node boot."""
    if not restarted:
        return ""

    def newest_first(r: dict) -> tuple[int, int]:
        return (0 if r["recent"] else 1, r["minutes"] if r["minutes"] is not None else UNKNOWN_RESTART_MINUTES)

    ranked = sorted(restarted, key=newest_first)
    named = ", ".join(
        f"{r['pod']} {r['restarts']}x, last {r['last_restart']}" + (" (in window)" if r["recent"] else "")
        for r in ranked[:SYSTEM_RESTARTS_NAMED]
    )
    more = len(ranked) - SYSTEM_RESTARTS_NAMED
    return f", restarted pods: {named}" + (f" (+{more} more; --include-system lists them)" if more > 0 else "")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Read-only cluster-wide workload triage: unhealthy pods, controllers below desired, recent Warning events.",
        allow_abbrev=False,
    )
    p.add_argument("--include-system", action="store_true", help="list system namespaces instead of summarising them")
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    return p


def main(
    argv: list[str] | None = None,
    run=run_kubectl,
    now: datetime | None = None,
    context: str | None = None,
    *,
    clock: Callable[[], float] = time.monotonic,
    budget_seconds: float | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    report = triage(
        run,
        now or datetime.now(timezone.utc),
        args.include_system,
        DEFAULT_INIT_GRACE_MINUTES,
        DEFAULT_EVENTS_MINUTES,
        context=context,
        clock=clock,
        budget_seconds=budget_seconds,
    )
    for err in report.get("errors") or []:
        print(f"warning: {err}", file=sys.stderr)
    if "summary" not in report:
        return EXIT_READ_FAILED
    if args.json:
        print(json.dumps(report, indent=1))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
