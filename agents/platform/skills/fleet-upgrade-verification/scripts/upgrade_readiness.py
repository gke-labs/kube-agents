#!/usr/bin/env python3
"""
upgrade_readiness.py — the rules behind `fleet_upgrade_report.py --readiness`.

Pure functions over data the report script has already read: the PodDisruptionBudgets
and workloads of one cluster, its `maintenancePolicy`, and its node-pool versions. Nothing
here runs a command or touches a clock; the caller passes the instant to evaluate at. The
three rules are the ones the governance SOPs define in prose:

- a drain-blocking PDB, `obtainability_audit_sop.md` §3.4: `maxUnavailable` 0 or `0%`, or
  `minAvailable` at or above the matched workloads' replica total (an integer, or a
  percentage that rounds up to it the way the disruption controller rounds);
- a maintenance exclusion in effect whose scope covers the upgrade the target needs,
  `security_patch_orchestrator_sop.md` §3.8, and the maintenance window's state at the
  instant, §3.7;
- node-pool version skew against the target control plane, §3.2: more than two minors, or
  a different major, blocks the control-plane upgrade until the pool moves.
"""

import re
from datetime import datetime, time, timedelta, timezone

# Per-member verdicts. `blocked` when any rule blocks; `unknown` when a rule could not be
# evaluated (the cluster read failed, or there is no target to grade against) and nothing
# blocked outright; `ready` otherwise.
READINESS_READY = "ready"
READINESS_BLOCKED = "blocked"
READINESS_UNKNOWN = "unknown"
READINESS_ORDER = (READINESS_BLOCKED, READINESS_READY, READINESS_UNKNOWN)

# The workload kinds a PDB is matched against. A DaemonSet is never here: a drain deletes
# its pods rather than evicting them, so a PDB on one blocks nothing (SOP §3.3).
WORKLOAD_KINDS = ("Deployment", "StatefulSet")
PDB_KIND = "PodDisruptionBudget"
# `spec.replicas` absent means one replica, the API default.
DEFAULT_REPLICAS = 1
# matchExpressions operators, as the LabelSelectorRequirement API names them.
OP_IN = "In"
OP_NOT_IN = "NotIn"
OP_EXISTS = "Exists"
OP_DOES_NOT_EXIST = "DoesNotExist"
PERCENT_SUFFIX = "%"
PERCENT_BASE = 100
# How a finding names its PDB and its workloads.
PDB_NAME_FORMAT = "{namespace}/{name}"
WORKLOAD_FORMAT = "{kind} {namespace}/{name} ({replicas} replicas)"
FIELD_MAX_UNAVAILABLE = "maxUnavailable: {value}"
FIELD_MIN_AVAILABLE_INT = "minAvailable: {value} (>= {total} expected pods)"
FIELD_MIN_AVAILABLE_PERCENT = "minAvailable: {value} (rounds up to {healthy} of {total} expected pods)"

# Exclusion scopes, as `maintenanceExclusionOptions.scope` spells them. An exclusion with
# no options block is NO_UPGRADES, the API default.
SCOPE_NO_UPGRADES = "NO_UPGRADES"
SCOPE_NO_MINOR_UPGRADES = "NO_MINOR_UPGRADES"
SCOPE_NO_MINOR_OR_NODE_UPGRADES = "NO_MINOR_OR_NODE_UPGRADES"
DEFAULT_EXCLUSION_SCOPE = SCOPE_NO_UPGRADES
KNOWN_SCOPES = (SCOPE_NO_UPGRADES, SCOPE_NO_MINOR_UPGRADES, SCOPE_NO_MINOR_OR_NODE_UPGRADES)
# What an exclusion verdict says. An exclusion holds back GKE's automatic upgrades only;
# an operator running `gcloud container clusters upgrade` by hand is not subject to it,
# which is why the text names auto-upgrade rather than the upgrade.
EXCLUSION_BLOCKS = "blocks auto-upgrade to {target} until {end}: {why}"
EXCLUSION_NOT_APPLICABLE = "in effect until {end} but its scope does not cover this upgrade ({why})"
EXCLUSION_TARGET_UNKNOWN = "in effect until {end}; whether its scope covers the upgrade needs a target"
EXCLUSION_UNKNOWN_SCOPE = "in effect until {end} with an unrecognised scope; not evaluated"
EXCLUSION_UNPARSABLE = "start or end time unparsable; not evaluated"
WHY_ANY_UPGRADE = "the scope covers every upgrade"
WHY_MINOR_UPGRADE = "the upgrade is a minor upgrade for {components}"
WHY_NODE_UPGRADE = "pool(s) {pools} need a node upgrade"
WHY_PATCH_ONLY = "patch-only upgrade"
WHY_NOTHING_TO_UPGRADE = "no component is below the target"
CONTROL_PLANE_LABEL = "the control plane"
POOL_LABEL = "pool {name}"
COMPONENT_SEPARATOR = " and "
LIST_SEPARATOR = ", "

# Maintenance window kinds and states. `not evaluated` is a recurrence outside the two
# forms handled here; it is never a verdict either way.
WINDOW_NONE = "none"
WINDOW_DAILY = "daily"
WINDOW_RECURRING = "recurring"
WINDOW_OPEN = "open"
WINDOW_CLOSED = "closed"
WINDOW_NOT_EVALUATED = "not evaluated"
# A daily window is always four hours; the record says so in `duration` as an ISO 8601
# duration, and a record without it gets the same value.
DAILY_WINDOW_HOURS = 4
DAILY_START_FORMAT = "%H:%M"
ISO_DURATION_RE = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$")
# RRULE support: `FREQ=DAILY`, and `FREQ=WEEKLY` with an optional plain `BYDAY` list; an
# `INTERVAL` other than 1, an ordinal BYDAY (`1SA`), or any other key or frequency is not
# evaluated. The two supported forms are the ones GKE's console and the SOP's remediation
# write.
RRULE_SEPARATOR = ";"
RRULE_KEY_VALUE_SEPARATOR = "="
RRULE_FREQ = "FREQ"
RRULE_BYDAY = "BYDAY"
RRULE_INTERVAL = "INTERVAL"
RRULE_DAILY = "DAILY"
RRULE_WEEKLY = "WEEKLY"
RRULE_DEFAULT_INTERVAL = "1"
BYDAY_SEPARATOR = ","
WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
DAYS_PER_WEEK = 7
SECONDS_PER_HOUR = 3600
# How far either side of the instant occurrences are generated: one week covers every
# weekly rule, plus the window's own length for a window that started before the range.
OCCURRENCE_HORIZON = timedelta(days=DAYS_PER_WEEK)
WINDOW_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%MZ"
# RFC 3339 proper: date, `T`, time, and an offset or `Z`. `datetime.fromisoformat` alone
# also takes a bare date, the basic form and a naive time, and a bare `--at 2026-09-14`
# read as midnight would move an exclusion or window verdict by up to a day unnoticed.
RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$")

# Version skew, SOP §3.2: GKE keeps nodes within two minors of the control plane, so a
# pool more than two behind the target (or on another major) blocks the control-plane
# upgrade until the pool moves; exactly two is at the ceiling and worth a note.
SKEW_CEILING_MINORS = 2
SKEW_BLOCKS = "blocks"
SKEW_AT_CEILING = "at ceiling"
SKEW_OK = "ok"
SKEW_UNKNOWN = "unknown"
SKEW_NOT_APPLICABLE = "n/a"
SKEW_AUTOPILOT_REASON = "Autopilot: Google owns the node pools"
SKEW_NO_TARGET_REASON = "no target to measure against"
SKEW_MAJOR_DIFFERS = "major version differs from the target"


# ---------------------------------------------------------------------------- PDBs


def split_items(items: list) -> tuple[list[dict], list[dict]]:
    """(pdbs, workloads) from the mixed `items` of `kubectl get pdb,deploy,statefulset -A`."""
    pdbs, workloads = [], []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind == PDB_KIND:
            pdbs.append(item)
        elif kind in WORKLOAD_KINDS:
            workloads.append(item)
    return pdbs, workloads


def selector_matches(selector, labels: dict) -> bool:
    """policy/v1 semantics: a null selector matches no pod, an empty one every pod."""
    if selector is None:
        return False
    if not isinstance(selector, dict):
        return False
    labels = labels if isinstance(labels, dict) else {}
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    for req in selector.get("matchExpressions") or []:
        if not isinstance(req, dict):
            return False
        key, op, values = req.get("key"), req.get("operator"), req.get("values") or []
        if op == OP_IN:
            if key not in labels or labels[key] not in values:
                return False
        elif op == OP_NOT_IN:
            if key in labels and labels[key] in values:
                return False
        elif op == OP_EXISTS:
            if key not in labels:
                return False
        elif op == OP_DOES_NOT_EXIST:
            if key in labels:
                return False
        else:
            return False
    return True


def _percent(value) -> int | None:
    """`"75%"` -> 75; None for anything that is not a percentage string."""
    if isinstance(value, str) and value.endswith(PERCENT_SUFFIX):
        try:
            return int(value[: -len(PERCENT_SUFFIX)].strip())
        except ValueError:
            return None
    return None


def _integer(value) -> int | None:
    """An int, or a string holding one; None otherwise (booleans excluded)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _round_up(percent: int, total: int) -> int:
    """`ceil(percent * total / 100)`, the disruption controller's rounding for both fields."""
    return -(-percent * total // PERCENT_BASE)


def blocking_field(spec: dict, total_replicas: int) -> str | None:
    """The spec field that blocks every drain of `total_replicas` pods, or None.

    `maxUnavailable` 0 or `0%` allows no disruption at any scale; a positive percentage
    rounds up to at least one pod, so it never blocks. `minAvailable` blocks when it demands
    every expected pod: an integer at or above the total, or a percentage that rounds up to
    the total (`100%` always, `90%` on nine replicas too).
    """
    if "maxUnavailable" in spec:
        value = spec.get("maxUnavailable")
        if _integer(value) == 0 or _percent(value) == 0:
            return FIELD_MAX_UNAVAILABLE.format(value=value)
        return None
    if "minAvailable" in spec:
        value = spec.get("minAvailable")
        as_int = _integer(value)
        if as_int is not None:
            return FIELD_MIN_AVAILABLE_INT.format(value=value, total=total_replicas) if as_int >= total_replicas else None
        as_percent = _percent(value)
        if as_percent is not None:
            healthy = _round_up(as_percent, total_replicas)
            if healthy >= total_replicas:
                return FIELD_MIN_AVAILABLE_PERCENT.format(value=value, healthy=healthy, total=total_replicas)
    return None


def _workload_summary(workload: dict) -> dict:
    meta = workload.get("metadata") or {}
    replicas = _integer((workload.get("spec") or {}).get("replicas"))
    return {
        "kind": workload.get("kind"),
        "namespace": meta.get("namespace", ""),
        "name": meta.get("name", ""),
        "replicas": DEFAULT_REPLICAS if replicas is None else replicas,
    }


def grade_pdbs(pdbs: list[dict], workloads: list[dict]) -> dict:
    """Which PDBs block a node drain, and how many were skipped and why.

    A PDB is matched to the Deployments and StatefulSets in its namespace whose pod-template
    labels satisfy its selector; the replica total of those workloads is what the controller
    expects, and the spec is graded against it (SOP §3.4 decides on the spec, with
    `status.disruptionsAllowed` as corroboration). Skipped and counted rather than graded:
    a PDB whose `status.expectedPods` is 0 or whose matched workloads are all scaled to
    zero (`scaled_to_zero`), one that matches no workload and covers no pod (`orphan`), and
    one that covers pods but matches neither kind read here (`unmatched`), which the caller
    notes rather than grades so a PDB on another controller is never silently ready.
    """
    by_namespace: dict[str, list[dict]] = {}
    for workload in workloads:
        namespace = (workload.get("metadata") or {}).get("namespace", "")
        by_namespace.setdefault(namespace, []).append(workload)

    result = {"blocking": [], "scaled_to_zero": 0, "orphan": 0, "unmatched": 0, "evaluated": 0}
    for pdb in pdbs:
        meta = pdb.get("metadata") or {}
        spec = pdb.get("spec") or {}
        status = pdb.get("status") or {}
        namespace = meta.get("namespace", "")
        matched = [
            _workload_summary(w)
            for w in by_namespace.get(namespace, [])
            if selector_matches(spec.get("selector"), (((w.get("spec") or {}).get("template") or {}).get("metadata") or {}).get("labels"))
        ]
        expected_pods = _integer(status.get("expectedPods"))
        total = sum(w["replicas"] for w in matched)
        if not matched:
            if expected_pods:
                result["unmatched"] += 1
            else:
                result["orphan"] += 1
            continue
        if expected_pods == 0 or total == 0:
            result["scaled_to_zero"] += 1
            continue
        result["evaluated"] += 1
        # The controller counts every pod the selector covers, including pods of a kind
        # not read here (a bare ReplicaSet beside the Deployment); when its count is the
        # larger, that is the total minAvailable is measured against.
        field = blocking_field(spec, max(total, expected_pods or 0))
        if field is None:
            continue
        result["blocking"].append(
            {
                "pdb": PDB_NAME_FORMAT.format(namespace=namespace, name=meta.get("name", "")),
                "namespace": namespace,
                "name": meta.get("name", ""),
                "field": field,
                "workloads": matched,
                "expected_pods": expected_pods,
                "disruptions_allowed": _integer(status.get("disruptionsAllowed")),
            }
        )
    return result


def describe_finding(finding: dict) -> str:
    """`ns/name (maxUnavailable: 0; Deployment ns/web (3 replicas))` for a table cell."""
    workloads = LIST_SEPARATOR.join(WORKLOAD_FORMAT.format(**w) for w in finding["workloads"])
    return f"{finding['pdb']} ({finding['field']}; {workloads})"


# --------------------------------------------------------------------- maintenance


def parse_rfc3339(text) -> datetime | None:
    """An RFC 3339 timestamp as an aware UTC datetime; None when it does not parse."""
    if not isinstance(text, str) or not RFC3339_RE.match(text.strip()):
        return None
    try:
        parsed = datetime.fromisoformat(text.strip().upper())
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc)


def format_instant(when: datetime | None) -> str | None:
    return when.astimezone(timezone.utc).strftime(WINDOW_TIMESTAMP_FORMAT) if when else None


def parse_iso_duration(text) -> timedelta | None:
    """`PT4H0M0S` -> 4 hours; None when it does not parse."""
    if not isinstance(text, str):
        return None
    m = ISO_DURATION_RE.match(text.strip())
    if not m or not any(m.groups()):
        return None
    days, hours, minutes, seconds = m.groups()
    return timedelta(days=int(days or 0), hours=int(hours or 0), minutes=int(minutes or 0), seconds=float(seconds or 0))


def _minor(version: tuple) -> tuple[int, int]:
    return version[0], version[1]


def _components_needing_minor(target, master, pools: list[dict]) -> list[str]:
    """The components for which the upgrade to `target` is a minor upgrade."""
    components = []
    if master is not None and _minor(target) > _minor(master):
        components.append(CONTROL_PLANE_LABEL)
    for pool in pools:
        version = pool.get("parsed")
        if version is not None and _minor(target) > _minor(version):
            components.append(POOL_LABEL.format(name=pool.get("name", "")))
    return components


def _pools_needing_node_upgrade(target, pools: list[dict]) -> list[str]:
    return [pool.get("name", "") for pool in pools if pool.get("parsed") is not None and pool["parsed"] < target]


def evaluate_exclusion(name: str, exclusion: dict, at: datetime, target_text: str | None, target, master, pools: list[dict]) -> dict:
    """One exclusion at `at`: whether it is in effect and whether its scope covers the upgrade.

    `blocks` is True, False, or None when it could not be decided (no target, an unknown
    scope, unparsable times). `pools` carry `name` and `parsed` (a version tuple or None).
    """
    start = parse_rfc3339(exclusion.get("startTime"))
    end = parse_rfc3339(exclusion.get("endTime"))
    scope = (exclusion.get("maintenanceExclusionOptions") or {}).get("scope") or DEFAULT_EXCLUSION_SCOPE
    entry = {
        "name": name,
        "scope": scope,
        "start_time": exclusion.get("startTime"),
        "end_time": exclusion.get("endTime"),
        "in_effect": False,
        "blocks": False,
        "detail": "",
    }
    if start is None or end is None:
        entry["blocks"] = None
        entry["detail"] = EXCLUSION_UNPARSABLE
        return entry
    entry["in_effect"] = start <= at <= end
    if not entry["in_effect"]:
        return entry
    end_text = format_instant(end)
    if scope not in KNOWN_SCOPES:
        entry["blocks"] = None
        entry["detail"] = EXCLUSION_UNKNOWN_SCOPE.format(end=end_text)
        return entry
    if scope == SCOPE_NO_UPGRADES:
        entry["blocks"] = True
        entry["detail"] = EXCLUSION_BLOCKS.format(target=target_text or "?", end=end_text, why=WHY_ANY_UPGRADE)
        return entry
    if target is None:
        entry["blocks"] = None
        entry["detail"] = EXCLUSION_TARGET_UNKNOWN.format(end=end_text)
        return entry
    minor_components = _components_needing_minor(target, master, pools)
    if minor_components:
        entry["blocks"] = True
        why = WHY_MINOR_UPGRADE.format(components=COMPONENT_SEPARATOR.join(minor_components))
        entry["detail"] = EXCLUSION_BLOCKS.format(target=target_text, end=end_text, why=why)
        return entry
    if scope == SCOPE_NO_MINOR_OR_NODE_UPGRADES:
        node_pools = _pools_needing_node_upgrade(target, pools)
        if node_pools:
            entry["blocks"] = True
            why = WHY_NODE_UPGRADE.format(pools=LIST_SEPARATOR.join(node_pools))
            entry["detail"] = EXCLUSION_BLOCKS.format(target=target_text, end=end_text, why=why)
            return entry
    below = (master is not None and master < target) or _pools_needing_node_upgrade(target, pools)
    entry["detail"] = EXCLUSION_NOT_APPLICABLE.format(end=end_text, why=WHY_PATCH_ONLY if below else WHY_NOTHING_TO_UPGRADE)
    return entry


def parse_rrule(text) -> dict | None:
    """{"freq": ..., "bydays": [...]} for a supported recurrence; None otherwise."""
    if not isinstance(text, str) or not text.strip():
        return None
    fields = {}
    for part in text.strip().split(RRULE_SEPARATOR):
        if RRULE_KEY_VALUE_SEPARATOR not in part:
            return None
        key, value = part.split(RRULE_KEY_VALUE_SEPARATOR, 1)
        fields[key.strip().upper()] = value.strip().upper()
    if fields.get(RRULE_INTERVAL, RRULE_DEFAULT_INTERVAL) != RRULE_DEFAULT_INTERVAL:
        return None
    freq = fields.get(RRULE_FREQ)
    extra_keys = set(fields) - {RRULE_FREQ, RRULE_BYDAY, RRULE_INTERVAL}
    if extra_keys:
        return None
    if freq == RRULE_DAILY:
        return {"freq": RRULE_DAILY, "bydays": []} if RRULE_BYDAY not in fields else None
    if freq == RRULE_WEEKLY:
        bydays = [d for d in fields.get(RRULE_BYDAY, "").split(BYDAY_SEPARATOR) if d]
        if any(d not in WEEKDAYS for d in bydays):
            return None
        return {"freq": RRULE_WEEKLY, "bydays": bydays}
    return None


def _occurrences(first_start: datetime, bydays: list[str], at: datetime, duration: timedelta) -> list[datetime]:
    """Window starts around `at`: every day, or every listed weekday, at `first_start`'s time."""
    weekday_indexes = {WEEKDAYS.index(d) for d in bydays} if bydays else set(range(DAYS_PER_WEEK))
    lower = at - OCCURRENCE_HORIZON - duration
    upper = at + OCCURRENCE_HORIZON
    starts = []
    day = lower.date()
    while day <= upper.date():
        if day.weekday() in weekday_indexes:
            start = datetime.combine(day, first_start.timetz())
            if start >= first_start:
                starts.append(start)
        day += timedelta(days=1)
    return starts


def _window_state(starts: list[datetime], duration: timedelta, at: datetime, first_start: datetime) -> dict:
    """Open or closed at `at`, when it closes, and the next start after `at`.

    The starts cover one week either side of `at`; a window whose first occurrence is
    further out than that has that occurrence as its next opening.
    """
    open_until = None
    for start in starts:
        if start <= at < start + duration:
            open_until = start + duration
            break
    next_opening = min((s for s in starts if s > at), default=None)
    if next_opening is None and first_start > at:
        next_opening = first_start
    return {
        "state": WINDOW_OPEN if open_until else WINDOW_CLOSED,
        "closes_at": format_instant(open_until),
        "next_opening": format_instant(next_opening),
    }


def evaluate_window(window: dict, at: datetime) -> dict:
    """The maintenance window's kind and its state at `at`.

    Handles `dailyMaintenanceWindow` (a four-hour window at a UTC time of day) and
    `recurringWindow` with `FREQ=DAILY` or `FREQ=WEEKLY[;BYDAY=...]`, whose first
    occurrence and length come from its `window`. Anything else is `not evaluated`.
    """
    daily = (window or {}).get("dailyMaintenanceWindow")
    recurring = (window or {}).get("recurringWindow")
    result = {"kind": WINDOW_NONE, "recurrence": None, "state": WINDOW_NONE, "closes_at": None, "next_opening": None, "detail": ""}
    if isinstance(daily, dict):
        result["kind"] = WINDOW_DAILY
        try:
            start_time = datetime.strptime(str(daily.get("startTime", "")), DAILY_START_FORMAT).time()
        except ValueError:
            result["state"] = WINDOW_NOT_EVALUATED
            result["detail"] = f"daily window start {daily.get('startTime')!r} unparsable"
            return result
        duration = parse_iso_duration(daily.get("duration")) or timedelta(hours=DAILY_WINDOW_HOURS)
        first = datetime.combine(at.date() - OCCURRENCE_HORIZON, time(start_time.hour, start_time.minute, tzinfo=timezone.utc))
        result.update(_window_state(_occurrences(first, [], at, duration), duration, at, first))
        result["detail"] = f"daily at {start_time.strftime(DAILY_START_FORMAT)}Z for {int(duration.total_seconds() // SECONDS_PER_HOUR)}h"
        return result
    if isinstance(recurring, dict):
        result["kind"] = WINDOW_RECURRING
        result["recurrence"] = recurring.get("recurrence")
        rule = parse_rrule(recurring.get("recurrence"))
        span = recurring.get("window") or {}
        start = parse_rfc3339(span.get("startTime"))
        end = parse_rfc3339(span.get("endTime"))
        if rule is None or start is None or end is None or end <= start:
            result["state"] = WINDOW_NOT_EVALUATED
            result["detail"] = f"recurrence {recurring.get('recurrence')!r} not evaluated; only FREQ=DAILY and FREQ=WEEKLY[;BYDAY=...] are"
            return result
        duration = end - start
        # RFC 5545: a WEEKLY rule with no BYDAY recurs on DTSTART's weekday, not every day.
        bydays = rule["bydays"] or ([WEEKDAYS[start.weekday()]] if rule["freq"] == RRULE_WEEKLY else [])
        result.update(_window_state(_occurrences(start, bydays, at, duration), duration, at, start))
        days = LIST_SEPARATOR.join(bydays) if bydays else RRULE_DAILY.lower()
        result["detail"] = f"{days} from {start.strftime(DAILY_START_FORMAT)}Z for {int(duration.total_seconds() // SECONDS_PER_HOUR)}h"
        return result
    result["detail"] = "no maintenance window; automatic upgrades may start at any hour"
    return result


def evaluate_maintenance(policy: dict | None, at: datetime, target_text: str | None, target, master, pools: list[dict]) -> dict:
    """Every exclusion and the window of one cluster's `maintenancePolicy`, at `at`."""
    window = (policy or {}).get("window") or {}
    exclusions = window.get("maintenanceExclusions") or {}
    entries = [
        evaluate_exclusion(name, exclusion if isinstance(exclusion, dict) else {}, at, target_text, target, master, pools)
        for name, exclusion in sorted(exclusions.items())
    ]
    return {
        "exclusions": entries,
        "blocking_exclusions": [e["name"] for e in entries if e["blocks"] is True],
        "undecided_exclusions": [e["name"] for e in entries if e["blocks"] is None],
        "window": evaluate_window(window, at),
    }


# ---------------------------------------------------------------------------- skew


def evaluate_skew(target, pools: list[dict], autopilot: bool) -> dict:
    """Per pool, how many minors it trails the target control plane, and whether that blocks.

    `pools` carry `name`, `version` and `parsed`. Not applicable on Autopilot, where Google
    owns the pools; not evaluated without a target.
    """
    if autopilot:
        return {"applicable": False, "reason": SKEW_AUTOPILOT_REASON, "pools": [], "blocking": [], "at_ceiling": [], "unknown": []}
    if target is None:
        return {"applicable": True, "reason": SKEW_NO_TARGET_REASON, "pools": [], "blocking": [], "at_ceiling": [], "unknown": [p.get("name", "") for p in pools]}
    graded = []
    for pool in pools:
        version = pool.get("parsed")
        entry = {"name": pool.get("name", ""), "version": pool.get("version"), "minors_behind_target": None, "verdict": SKEW_UNKNOWN, "detail": ""}
        if version is None:
            entry["detail"] = f"version {pool.get('version')!r} unparsable"
        elif version[0] != target[0]:
            entry["verdict"] = SKEW_BLOCKS
            entry["detail"] = SKEW_MAJOR_DIFFERS
        else:
            behind = target[1] - version[1]
            entry["minors_behind_target"] = behind
            if behind > SKEW_CEILING_MINORS:
                entry["verdict"] = SKEW_BLOCKS
                entry["detail"] = f"{behind} minors behind the target control plane; more than {SKEW_CEILING_MINORS} blocks the control-plane upgrade until the pool moves"
            elif behind == SKEW_CEILING_MINORS:
                entry["verdict"] = SKEW_AT_CEILING
                entry["detail"] = f"{behind} minors behind the target control plane, at the skew ceiling; the next minor is blocked until the pool moves"
            else:
                entry["verdict"] = SKEW_OK
        graded.append(entry)
    return {
        "applicable": True,
        "reason": None,
        "pools": graded,
        "blocking": [p["name"] for p in graded if p["verdict"] == SKEW_BLOCKS],
        "at_ceiling": [p["name"] for p in graded if p["verdict"] == SKEW_AT_CEILING],
        "unknown": [p["name"] for p in graded if p["verdict"] == SKEW_UNKNOWN],
    }


# ------------------------------------------------------------------------- verdict


def readiness_status(pdbs: dict | None, maintenance: dict, skew: dict, target_known: bool) -> str:
    """`blocked` beats `unknown` beats `ready`: a definite blocker is reported whatever else
    could not be evaluated, and a member is `ready` only when every rule was evaluated."""
    if (pdbs and pdbs["blocking"]) or maintenance["blocking_exclusions"] or skew["blocking"]:
        return READINESS_BLOCKED
    if pdbs is None or not target_known or maintenance["undecided_exclusions"] or skew["unknown"]:
        return READINESS_UNKNOWN
    return READINESS_READY
