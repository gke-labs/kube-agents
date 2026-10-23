#!/usr/bin/env python3
"""workload_triage.py -- one read-only pass over a cluster's workload health.

The Cluster Agent's ``workload-triage-scan`` skill runs this once to answer
"what is failing?" or "is this cluster healthy?" without composing a command per
pod. It makes three ``kubectl get -o json`` reads, all namespaces at once:

1. pods,
2. Deployments, StatefulSets and DaemonSets,
3. Warning events.

It prints only what needs attention:

- unhealthy pods, with phase, reason, ready count, restarts, last exit code and
  the focused skill that owns the symptom;
- pods that are still starting, reported as initializing and never waited on;
- pods that are Ready now but restarted before the events window, and pods
  that failed before it and belong to a controller that replaces them (an
  evicted ReplicaSet pod), listed as history and not counted;
- controllers whose ready count is below the desired count;
- Warning events seen within ``--events-minutes`` (default 60), grouped by
  object and reason.

System namespaces (``kube-*``, ``gke-*``, ``gmp-*`` and ``*-system``) are folded
into one summary line each unless ``--include-system`` is given, so a restart in
a managed add-on does not pull the agent into exploring it. A system line names
its restarted pods with count and age, newest restart first.

The first line names the kubeconfig context and the UTC time of the scan, so
the output is its own audit trail. The script never mutates and never sleeps.
It always ends with
``unhealthy: <count>`` -- unhealthy pods plus controllers below desired, outside
system namespaces -- which is ``0`` on a healthy cluster.

Usage::

    workload_triage.py
    workload_triage.py --include-system
    workload_triage.py --json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

KUBECTL = "kubectl"
PODS_ARGS = ("get", "pods", "--all-namespaces", "-o", "json")
CONTROLLERS_ARGS = ("get", "deployments,statefulsets,daemonsets", "--all-namespaces", "-o", "json")
CONTEXT_ARGS = ("config", "current-context")
EVENTS_ARGS = ("get", "events", "--all-namespaces", "--field-selector", "type=Warning", "-o", "json")

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
DEFAULT_MAX_ROWS = 25
EVENT_MESSAGE_CHARS = 140
# How many restarted pods a system namespace's summary line names, most
# restarts first; the rest are counted.
SYSTEM_RESTARTS_NAMED = 5

# The focused skill each symptom hands off to.
SKILL_CRASHLOOP = "pod-crashloop-oom"
SKILL_PENDING = "pod-pending-scheduling"
SKILL_CREATING = "pod-container-creating"

CRASH_WAITING_REASONS = frozenset({"CrashLoopBackOff", "RunContainerError"})
IMAGE_WAITING_REASONS = frozenset({"ImagePullBackOff", "ErrImagePull", "InvalidImageName", "ErrImageNeverPull"})
CONFIG_WAITING_REASONS = frozenset({"CreateContainerConfigError", "CreateContainerError"})
STARTING_WAITING_REASONS = frozenset({"ContainerCreating", "PodInitializing"})

STATUS_INITIALIZING = "initializing"

# What a pod row is for. Only CATEGORY_UNHEALTHY rows count toward `unhealthy:`
# and name a skill; the others are context the answer quotes without digging.
CATEGORY_UNHEALTHY = "unhealthy"
CATEGORY_INITIALIZING = "initializing"
# Running and Ready now, restarted before the events window: the restart count
# a "find the restarting pods" question asks for, not a live failure.
CATEGORY_RESTARTED_EARLIER = "restarted_earlier"
# Failed (an eviction, say) before the events window, owned by a controller
# that replaces its pods. The pod object lingers until garbage collection; a
# shortfall in its replacement shows in CONTROLLERS BELOW DESIRED instead.
CATEGORY_STALE_FAILED = "stale_failed"
REPLACING_OWNER_KINDS = frozenset({"ReplicaSet", "StatefulSet", "DaemonSet"})


def run_kubectl(args: tuple[str, ...]) -> tuple[int, str, str]:
    """Run one kubectl read; a missing binary is a failed read, not a crash."""
    try:
        res = subprocess.run([KUBECTL, *args], capture_output=True, text=True, check=False)
    except OSError as exc:
        return 127, "", str(exc)
    return res.returncode, res.stdout, res.stderr


def current_context(run) -> str:
    """The kubeconfig context the reads used, for the report's audit line; a
    local kubeconfig read, not an API call."""
    code, out, _ = run(CONTEXT_ARGS)
    return out.strip() if code == 0 and out.strip() else "unknown"


def read_items(run, args: tuple[str, ...], what: str, errors: list[str]) -> list[dict] | None:
    """The items of one List read, or None (with the reason in errors) when it failed."""
    code, out, err = run(args)
    if code != 0:
        errors.append(f"cannot read {what}: {(err or out).strip()[:200]}")
        return None
    try:
        return json.loads(out).get("items") or []
    except (ValueError, AttributeError):
        errors.append(f"cannot parse {what} output as JSON")
        return None


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def format_age(start: datetime | None, now: datetime) -> str:
    if start is None:
        return "?"
    minutes = int((now - start).total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 0)}m"
    if minutes < 48 * 60:
        return f"{minutes // 60}h"
    return f"{minutes // (24 * 60)}d"


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


def classify_pod(pod: dict, now: datetime, grace: timedelta, window: timedelta) -> dict | None:
    """One row for a pod that needs attention, or None for a healthy or finished pod.

    The row's ``category`` says what it is: ``unhealthy`` rows name the ``skill``
    that owns their symptom; ``initializing`` rows are pods still starting inside
    the grace period; ``restarted_earlier`` and ``stale_failed`` rows are history
    from before the events window, reported but not counted.
    """
    meta = pod.get("metadata", {})
    status = pod.get("status", {})
    phase = status.get("phase") or "Unknown"
    if phase == "Succeeded":
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
    row = {
        "namespace": meta.get("namespace", ""),
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
        row["last_restart_minutes"] = int((now - last_finished).total_seconds() // 60) if last_finished else None

    if phase == "Failed":
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
        # History only when a controller replaces the pod. A Job's or a bare
        # pod's failure stays a failure however old it is.
        owners = {o.get("kind") for o in meta.get("ownerReferences") or []}
        replaced = bool(owners & REPLACING_OWNER_KINDS)
        category = CATEGORY_STALE_FAILED if stale and replaced else None
        return verdict(status.get("reason") or "Failed", SKILL_CRASHLOOP, category)

    waiting = [((cs.get("state") or {}).get("waiting") or {}).get("reason") or "" for cs in all_statuses]
    for reason in waiting:
        if reason in CRASH_WAITING_REASONS:
            return verdict(reason, SKILL_CRASHLOOP)
    for reason in waiting:
        if reason in IMAGE_WAITING_REASONS or reason in CONFIG_WAITING_REASONS:
            return verdict(reason, SKILL_CREATING)

    scheduled = _condition(pod, "PodScheduled")
    if phase == "Pending" and scheduled.get("status") == "False":
        reason = scheduled.get("reason") or "Unschedulable"
        # Unschedulable while a node is being added is still starting.
        return verdict(reason, None if young else SKILL_PENDING)

    for reason in waiting:
        if reason in STARTING_WAITING_REASONS:
            return verdict(reason, None if young else SKILL_CREATING)

    if phase == "Pending":
        return verdict("Pending", None if young else SKILL_PENDING)

    if _condition(pod, "Ready").get("status") != "True":
        return verdict("NotReady", None if young else SKILL_CRASHLOOP)

    # Running and Ready, but restarted inside the events window: an intermittent crash.
    if restarts and last_finished is not None and now - last_finished < window:
        return verdict("RecentRestart", SKILL_CRASHLOOP)
    if restarts:
        return verdict("RestartedEarlier", SKILL_CRASHLOOP, CATEGORY_RESTARTED_EARLIER)
    return None


def classify_controller(obj: dict) -> dict | None:
    """A row for a controller whose ready count is below its desired count."""
    kind = obj.get("kind", "")
    spec = obj.get("spec", {})
    status = obj.get("status", {})
    if kind == "DaemonSet":
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
    meta = obj.get("metadata", {})
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


def triage(run, now: datetime, include_system: bool, grace_minutes: int, events_minutes: int) -> dict:
    errors: list[str] = []
    grace = timedelta(minutes=grace_minutes)
    window = timedelta(minutes=events_minutes)
    pods = read_items(run, PODS_ARGS, "pods", errors)
    if pods is None:
        return {"errors": errors}
    controllers = read_items(run, CONTROLLERS_ARGS, "deployments, statefulsets and daemonsets", errors)
    events = read_items(run, EVENTS_ARGS, "warning events", errors)

    def shown(namespace: str) -> bool:
        return include_system or not is_system_namespace(namespace)

    system: dict[str, dict] = {}

    def system_entry(namespace: str) -> dict:
        return system.setdefault(
            namespace, {"pods": 0, "unhealthy": 0, "reasons": {}, "controllers_short": 0, "warnings": 0, "restarted": []}
        )

    def has_findings(e: dict) -> bool:
        return bool(e["unhealthy"] or e["controllers_short"] or e["warnings"] or e["restarted"])

    rows: dict[str, list[dict]] = {
        CATEGORY_UNHEALTHY: [],
        CATEGORY_INITIALIZING: [],
        CATEGORY_RESTARTED_EARLIER: [],
        CATEGORY_STALE_FAILED: [],
    }
    counted = 0
    for pod in pods:
        namespace = pod.get("metadata", {}).get("namespace", "")
        if (pod.get("status") or {}).get("phase") == "Succeeded":
            continue
        row = classify_pod(pod, now, grace, window)
        if not shown(namespace):
            entry = system_entry(namespace)
            entry["pods"] += 1
            if row and row["category"] == CATEGORY_UNHEALTHY:
                entry["unhealthy"] += 1
                entry["reasons"][row["reason"]] = entry["reasons"].get(row["reason"], 0) + 1
            # Restart counts answer "which pods restarted?" without listing
            # the namespace, so they ride on the summary line.
            if row and row["restarts"]:
                entry["restarted"].append(
                    {
                        "pod": row["pod"],
                        "restarts": row["restarts"],
                        "last_restart": row.get("last_restart", "?"),
                        "minutes": row.get("last_restart_minutes"),
                        "recent": row["reason"] == "RecentRestart",
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
    for obj in controllers or []:
        row = classify_controller(obj)
        if row is None:
            continue
        if shown(row["namespace"]):
            short.append(row)
        else:
            system_entry(row["namespace"])["controllers_short"] += 1

    warnings = []
    node_events = {"count": 0, "nodes": set(), "reasons": {}}
    for row in group_events(events or [], now, window):
        if row["object"].startswith("Node/") and not include_system:
            node_events["count"] += row["count"]
            node_events["nodes"].add(row["object"])
            node_events["reasons"][row["reason"]] = node_events["reasons"].get(row["reason"], 0) + row["count"]
        elif shown(row["namespace"]):
            warnings.append(row)
        else:
            system_entry(row["namespace"])["warnings"] += row["count"]

    return {
        "scanned_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "context": current_context(run),
        "summary": {
            "pods": counted,
            "healthy": counted - len(unhealthy) - len(initializing) - len(stale_failed),
            "initializing": len(initializing),
            "unhealthy_pods": len(unhealthy),
            "restarted_earlier": len(restarted_earlier),
            "stale_failed": len(stale_failed),
            "controllers_below_desired": len(short),
            "warning_events": len(warnings),
            "events_minutes": events_minutes,
            "unhealthy": len(unhealthy) + len(short),
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


def render_text(report: dict, max_rows: int) -> str:
    s = report["summary"]
    out = [
        f"scanned {report['scanned_at']} context {report['context']}",
        f"pods: {s['pods']} (healthy {s['healthy']}, initializing {s['initializing']}, unhealthy {s['unhealthy_pods']}"
        f"{', failed earlier ' + str(s['stale_failed']) if s['stale_failed'] else ''})"
        f"  controllers below desired: {s['controllers_below_desired']}"
        f"  warning events ({s['events_minutes']}m): {s['warning_events']}"
    ]

    def section(title: str, headers: list[str], rows: list[list[str]]) -> None:
        if not rows:
            return
        out.append("")
        out.append(title)
        out.extend(_table(headers, rows[:max_rows]))
        if len(rows) > max_rows:
            out.append(f"... {len(rows) - max_rows} more (--max-rows or --json for all)")

    section(
        "UNHEALTHY PODS",
        ["NAMESPACE", "POD", "STATUS", "READY", "RESTARTS", "LAST EXIT", "AGE", "SKILL"],
        [[r["namespace"], r["pod"], r["status"], r["ready"], str(r["restarts"]), r["last_exit"], r["age"], r["skill"]] for r in report["unhealthy_pods"]],
    )
    section(
        "INITIALIZING (still starting; report as initializing, do not wait)",
        ["NAMESPACE", "POD", "REASON", "AGE"],
        [[r["namespace"], r["pod"], r["reason"], r["age"]] for r in report["initializing"]],
    )
    section(
        "RESTARTED EARLIER (Ready now; last restart before the events window; not counted)",
        ["NAMESPACE", "POD", "RESTARTS", "LAST EXIT", "LAST RESTART"],
        [[r["namespace"], r["pod"], str(r["restarts"]), r["last_exit"], r["last_restart"]] for r in report["restarted_earlier"]],
    )
    section(
        "FAILED EARLIER (ended before the events window; replaced by their controller; not counted)",
        ["NAMESPACE", "POD", "REASON", "LAST EXIT", "AGE"],
        [[r["namespace"], r["pod"], r["reason"], r["last_exit"], r["age"]] for r in report["stale_failed"]],
    )
    section(
        "CONTROLLERS BELOW DESIRED",
        ["NAMESPACE", "CONTROLLER", "DESIRED", "READY", "UPDATED"],
        [[r["namespace"], r["controller"], str(r["desired"]), str(r["ready"]), str(r["updated"])] for r in report["controllers_below_desired"]],
    )
    section(
        "WARNING EVENTS",
        ["NAMESPACE", "OBJECT", "REASON", "COUNT", "LAST SEEN", "MESSAGE"],
        [[r["namespace"], r["object"], r["reason"], str(r["count"]), r["last_seen"], r["message"]] for r in report["warning_events"]],
    )
    system = report["system_namespaces"]
    nodes = report["node_warning_events"]
    if system or report["system_namespaces_healthy"] or nodes["count"]:
        out.append("")
        out.append("SYSTEM NAMESPACES AND NODES (summarised; --include-system lists them)")
        if nodes["count"]:
            top = ", ".join(f"{k} {v}" for k, v in list(nodes["reasons"].items())[:5])
            out.append(f"nodes: {nodes['count']} warning events on {nodes['nodes']} nodes ({top})")
        for ns, e in system.items():
            reasons = ", ".join(f"{k} {v}" for k, v in sorted(e["reasons"].items()))
            out.append(
                f"{ns}: {e['unhealthy']}/{e['pods']} pods unhealthy"
                + (f" ({reasons})" if reasons else "")
                + f", {e['controllers_short']} controllers below desired, {e['warnings']} warning events"
                + _restarted_clause(e["restarted"])
            )
        if report["system_namespaces_healthy"]:
            out.append(f"no findings: {', '.join(report['system_namespaces_healthy'])}")
    out.append("")
    out.append(f"unhealthy: {s['unhealthy']}")
    return "\n".join(out)


def _restarted_clause(restarted: list[dict]) -> str:
    """Restarted system pods, newest restart first, so one that restarted in the
    events window is never crowded out by pods that restarted at node boot."""
    if not restarted:
        return ""

    def newest_first(r: dict) -> tuple[int, int]:
        return (0 if r["recent"] else 1, r["minutes"] if r["minutes"] is not None else 1 << 30)

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
    p.add_argument("--events-minutes", type=int, default=DEFAULT_EVENTS_MINUTES, help="Warning event window (default 60)")
    p.add_argument(
        "--init-grace-minutes",
        type=int,
        default=DEFAULT_INIT_GRACE_MINUTES,
        help="age under which a starting pod is initializing, not stuck (default 10)",
    )
    p.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS, help="rows per section in text output (default 25)")
    return p


def main(argv: list[str] | None = None, run=run_kubectl, now: datetime | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = triage(
        run,
        now or datetime.now(timezone.utc),
        args.include_system,
        args.init_grace_minutes,
        args.events_minutes,
    )
    for err in report.get("errors") or []:
        print(f"warning: {err}", file=sys.stderr)
    if "summary" not in report:
        return 2
    if args.json:
        print(json.dumps(report, indent=1))
    else:
        print(render_text(report, args.max_rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
