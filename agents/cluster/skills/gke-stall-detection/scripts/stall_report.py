#!/usr/bin/env python3
"""stall_report.py -- find controllers that stopped making progress without erroring.

Reads one namespace (or one kind in it) through ``kubectl get -o json`` and
applies four heuristics, each gated by an age threshold so that a controller
that is merely slow is not reported:

``generation-lag``
    ``metadata.generation`` is ahead of ``status.observedGeneration``, and the
    newest spec-writing ``managedFields`` entry is older than the threshold.
``stale-condition``
    A progress condition (``Progressing``, ``Accepted``, ``Programmed``,
    ``ResolvedRefs``, ``Ready``, ``Available``) is not ``True`` and its
    ``lastTransitionTime`` is older than the threshold. Conditions nested under
    ``status`` (Gateway listeners, HTTPRoute parents) are read too.
``repeating-warnings``
    A Warning event on the object has been repeating for longer than the
    threshold and was still firing within the last threshold window.
``dangling-reference``
    The spec names an object that does not exist -- a listener's
    ``certificateRefs`` Secret, an ``envFrom`` ConfigMap, a route's parent
    Gateway or backend Service -- and the spec has been that way for longer
    than the threshold. Referents are resolved by name only; Secret contents
    are never read.

The script never mutates. It prints a table (or ``--json``) and always ends
with ``stalled resources: <count>``, ``0`` on a healthy namespace.

Usage::

    stall_report.py --namespace payments
    stall_report.py --namespace payments --kind gateways,httproutes
    stall_report.py --namespace payments --threshold-minutes 60 --json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

KUBECTL = "kubectl"

# How long an object may sit without progress before it is a stall. One default
# for every kind, with per-kind overrides where the API itself names a horizon:
# a Deployment's progressDeadlineSeconds defaults to 600, so a rollout still
# not complete after ten minutes has already exceeded what its own controller
# tolerates. --threshold-minutes overrides both for one run.
DEFAULT_STALL_MINUTES = 15
STALL_MINUTES_BY_KIND: dict[str, int] = {
    "Deployment": 10,
}

# Condition types whose status is expected to reach "True" on a settled object.
# Anything else on an object's conditions list (ReplicaFailure, ScalingLimited,
# Complete) either signals in the other direction or is terminal, and is left
# to the skills that own those symptoms.
PROGRESS_CONDITION_TYPES = frozenset(
    {"Progressing", "Accepted", "Programmed", "ResolvedRefs", "Ready", "Available"}
)
CONDITION_TRUE = "True"

# A Pod that has finished is not stalled however long its Ready condition has
# been False, so terminal phases are skipped entirely.
TERMINAL_POD_PHASES = frozenset({"Succeeded", "Failed"})

# What counts as a repeating warning in one snapshot: at least this many
# occurrences, spanning at least the threshold, the newest inside the last
# threshold window. A single snapshot cannot watch a count rise; the span and
# the recency together are the closest a single read gets.
WARNING_EVENT_TYPE = "Warning"
REPEATING_EVENT_MIN_COUNT = 3

# managedFields operations that write spec. "Update" also covers status writes
# by controllers, so an entry only counts when its fieldsV1 touches f:spec.
SPEC_WRITE_OPERATIONS = frozenset({"Apply", "Update", "Create"})
SPEC_FIELD_KEY = "f:spec"

# Resources a whole-namespace scan skips. Events are read separately for the
# repeating-warnings heuristic; Secrets are never fetched as objects (their
# names come from `-o name` when a reference needs resolving); metrics are
# samples, not reconciled objects.
SCAN_EXCLUDED_RESOURCES = frozenset({"events", "events.events.k8s.io", "secrets"})
SCAN_EXCLUDED_GROUPS = frozenset({"metrics.k8s.io"})

# Spec keys that carry a reference to another object, with the kind a reference
# defaults to when it does not name one. Each value is a dict or a list of
# dicts carrying `name`, optionally `kind`, `namespace` and `optional`.
REFERENCE_KEYS: dict[str, str] = {
    "certificateRefs": "Secret",
    "configMapRef": "ConfigMap",
    "configMapKeyRef": "ConfigMap",
    "secretRef": "Secret",
    "secretKeyRef": "Secret",
    "parentRefs": "Gateway",
    "backendRefs": "Service",
}
# Pod volume sources name their referent under a source-specific key.
VOLUME_REFERENCE_KEYS: dict[str, tuple[str, str]] = {
    "configMap": ("ConfigMap", "name"),
    "secret": ("Secret", "secretName"),
    "persistentVolumeClaim": ("PersistentVolumeClaim", "claimName"),
}
# The kubectl resource name each referent kind resolves through. A reference
# to a kind not listed here is left alone rather than reported missing.
REFERENT_RESOURCES: dict[str, str] = {
    "Secret": "secrets",
    "ConfigMap": "configmaps",
    "Service": "services",
    "Gateway": "gateways.gateway.networking.k8s.io",
    "PersistentVolumeClaim": "persistentvolumeclaims",
    "ServiceAccount": "serviceaccounts",
}

# Object kinds a scan never evaluates: Secrets are never fetched as objects
# and Events are inputs to a heuristic rather than subjects of one.
SKIPPED_OBJECT_KINDS = frozenset({"Secret", "Event"})
# The status key every conditions list hangs from, at any depth.
CONDITIONS_KEY = "conditions"

HEURISTIC_GENERATION = "generation-lag"
HEURISTIC_CONDITION = "stale-condition"
HEURISTIC_EVENTS = "repeating-warnings"
HEURISTIC_REFERENCE = "dangling-reference"

TABLE_COLUMNS = ("OBJECT", "HEURISTIC", "DETAIL", "STALLED_FOR")
SUMMARY_LINE = "stalled resources: {count}"
MAX_DETAIL_CHARS = 100
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400

EXIT_OK = 0
EXIT_ERROR = 2


# --------------------------------------------------------------------------
# kubectl
# --------------------------------------------------------------------------


def run_kubectl(args: list[str]) -> tuple[int, str, str]:
    """Run kubectl and return (rc, stdout, stderr). Never raises."""
    try:
        res = subprocess.run([KUBECTL, *args], capture_output=True, text=True, check=False)
    except OSError as exc:
        return -1, "", str(exc)
    return res.returncode, res.stdout, res.stderr


def kubectl_json(args: list[str]) -> dict:
    """Run `kubectl ... -o json` and parse it.

    kubectl asked for several resource types at once still prints the ones it
    could read when one of them fails, exiting non-zero. Parseable output wins
    and the failure is a warning; only unparseable output is an error.
    """
    rc, stdout, stderr = run_kubectl([*args, "-o", "json"])
    for line in stderr.splitlines():
        if line.strip():
            sys.stderr.write(f"warning: {line}\n")
    if not stdout.strip():
        if rc != 0:
            raise RuntimeError(f"kubectl {' '.join(args)} failed ({rc}): {stderr.strip()}")
        return {"items": []}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"kubectl {' '.join(args)} returned unparseable output ({rc}): {exc}")


def kubectl_names(resource: str, namespace: str) -> set[str] | None:
    """Names of `resource` in `namespace`, or None when the API cannot be read.

    `-o name` carries no object bodies, which is what keeps Secret contents
    out of this script.
    """
    rc, stdout, stderr = run_kubectl(["get", resource, "-n", namespace, "-o", "name"])
    if rc != 0:
        sys.stderr.write(f"warning: cannot list {resource} in {namespace}: {stderr.strip()}\n")
        return None
    return {line.split("/", 1)[-1] for line in stdout.splitlines() if line.strip()}


def namespaced_resources() -> list[str]:
    """Every listable namespaced resource, minus the scan exclusions."""
    rc, stdout, stderr = run_kubectl(
        ["api-resources", "--namespaced=true", "--verbs=list", "-o", "name"]
    )
    if rc != 0:
        raise RuntimeError(f"kubectl api-resources failed ({rc}): {stderr.strip()}")
    resources = []
    for line in stdout.splitlines():
        name = line.strip()
        if not name or name in SCAN_EXCLUDED_RESOURCES:
            continue
        group = name.split(".", 1)[1] if "." in name else ""
        if group in SCAN_EXCLUDED_GROUPS:
            continue
        resources.append(name)
    return resources


class NameResolver:
    """Answers "does Kind/name exist in namespace" from `-o name` listings, cached."""

    def __init__(self, lister=kubectl_names):
        self._lister = lister
        self._cache: dict[tuple[str, str], set[str] | None] = {}

    def exists(self, kind: str, namespace: str, name: str) -> bool | None:
        """True/False when the kind is resolvable, None when it is not."""
        resource = REFERENT_RESOURCES.get(kind)
        if resource is None:
            return None
        key = (resource, namespace)
        if key not in self._cache:
            self._cache[key] = self._lister(resource, namespace)
        names = self._cache[key]
        if names is None:
            return None
        return name in names


# --------------------------------------------------------------------------
# time helpers
# --------------------------------------------------------------------------


def parse_time(value) -> datetime | None:
    """Parse a Kubernetes RFC 3339 timestamp; None when absent or malformed."""
    if not value or not isinstance(value, str):
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_duration(seconds: float) -> str:
    total = int(seconds)
    if total < SECONDS_PER_MINUTE:
        return "<1m"
    days, rem = divmod(total, SECONDS_PER_DAY)
    hours, rem = divmod(rem, SECONDS_PER_HOUR)
    minutes = rem // SECONDS_PER_MINUTE
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def spec_write_time(obj: dict) -> datetime | None:
    """When the spec was last written: newest spec-touching managedFields
    entry, falling back to creationTimestamp."""
    meta = obj.get("metadata") or {}
    newest = None
    for entry in meta.get("managedFields") or []:
        if entry.get("operation") not in SPEC_WRITE_OPERATIONS:
            continue
        fields = entry.get("fieldsV1") or {}
        if SPEC_FIELD_KEY not in fields:
            continue
        stamp = parse_time(entry.get("time"))
        if stamp and (newest is None or stamp > newest):
            newest = stamp
    return newest or parse_time(meta.get("creationTimestamp"))


# --------------------------------------------------------------------------
# heuristics
# --------------------------------------------------------------------------


def threshold_for(kind: str, override_minutes: int | None) -> timedelta:
    if override_minutes is not None:
        minutes = override_minutes
    else:
        minutes = STALL_MINUTES_BY_KIND.get(kind, DEFAULT_STALL_MINUTES)
    return timedelta(minutes=minutes)


def object_label(obj: dict) -> str:
    return f"{obj.get('kind', '?')}/{(obj.get('metadata') or {}).get('name', '?')}"


def finding(obj: dict, heuristic: str, detail: str, stalled: timedelta) -> dict:
    seconds = int(stalled.total_seconds())
    return {
        "object": object_label(obj),
        "namespace": (obj.get("metadata") or {}).get("namespace", ""),
        "heuristic": heuristic,
        "detail": detail[:MAX_DETAIL_CHARS],
        "stalled_for": format_duration(seconds),
        "stalled_seconds": seconds,
    }


def check_generation_lag(obj: dict, now: datetime, threshold: timedelta) -> list[dict]:
    meta = obj.get("metadata") or {}
    status = obj.get("status")
    if not isinstance(status, dict) or "observedGeneration" not in status:
        return []
    generation = meta.get("generation")
    observed = status.get("observedGeneration")
    if not isinstance(generation, int) or not isinstance(observed, int):
        return []
    if generation <= observed:
        return []
    written = spec_write_time(obj)
    if written is None:
        return []
    age = now - written
    if age < threshold:
        return []
    detail = f"generation {generation}, observedGeneration {observed}"
    return [finding(obj, HEURISTIC_GENERATION, detail, age)]


def iter_conditions(node, path: str = ""):
    """Yield (path, condition) for every `conditions` list under a status,
    including nested ones such as Gateway listeners and HTTPRoute parents."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == CONDITIONS_KEY and isinstance(value, list):
                for cond in value:
                    if isinstance(cond, dict):
                        yield path, cond
            else:
                yield from iter_conditions(value, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            name = item.get("name") if isinstance(item, dict) else None
            label = f"{path}[{name if name else index}]"
            yield from iter_conditions(item, label)


def check_stale_conditions(obj: dict, now: datetime, threshold: timedelta) -> list[dict]:
    status = obj.get("status")
    if not isinstance(status, dict):
        return []
    if obj.get("kind") == "Pod" and status.get("phase") in TERMINAL_POD_PHASES:
        return []
    findings = []
    for path, cond in iter_conditions(status):
        ctype = cond.get("type")
        if ctype not in PROGRESS_CONDITION_TYPES or cond.get("status") == CONDITION_TRUE:
            continue
        since = parse_time(cond.get("lastTransitionTime"))
        if since is None:
            continue
        age = now - since
        if age < threshold:
            continue
        where = f"{path} " if path else ""
        reason = cond.get("reason") or ""
        detail = f"{where}{ctype}={cond.get('status')} {reason}".strip()
        findings.append(finding(obj, HEURISTIC_CONDITION, detail, age))
    return findings


def event_count(event: dict) -> int:
    series = event.get("series") or {}
    return int(event.get("count") or series.get("count") or 1)


def event_span(event: dict) -> tuple[datetime | None, datetime | None]:
    series = event.get("series") or {}
    first = parse_time(event.get("firstTimestamp")) or parse_time(event.get("eventTime"))
    last = (
        parse_time(event.get("lastTimestamp"))
        or parse_time(series.get("lastObservedTime"))
        or parse_time(event.get("eventTime"))
    )
    return first, last


def index_events(events: list[dict]) -> dict[tuple[str, str], list[dict]]:
    by_object: dict[tuple[str, str], list[dict]] = {}
    for event in events:
        involved = event.get("involvedObject") or event.get("regarding") or {}
        key = (involved.get("kind", ""), involved.get("name", ""))
        by_object.setdefault(key, []).append(event)
    return by_object


def check_repeating_warnings(
    obj: dict, events: list[dict], now: datetime, threshold: timedelta
) -> list[dict]:
    findings = []
    for event in events:
        if event.get("type") != WARNING_EVENT_TYPE:
            continue
        if event_count(event) < REPEATING_EVENT_MIN_COUNT:
            continue
        first, last = event_span(event)
        if first is None or last is None:
            continue
        span = last - first
        if span < threshold or now - last > threshold:
            continue
        message = " ".join((event.get("message") or "").split())
        detail = f"{event.get('reason', '')} x{event_count(event)}: {message}"
        findings.append(finding(obj, HEURISTIC_EVENTS, detail, span))
    return findings


def iter_references(node, path: str = ""):
    """Yield (path, kind, name, namespace, optional) for every object
    reference under a spec."""
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else key
            if key in REFERENCE_KEYS:
                refs = value if isinstance(value, list) else [value]
                for ref in refs:
                    if isinstance(ref, dict) and ref.get("name"):
                        yield (
                            here,
                            ref.get("kind") or REFERENCE_KEYS[key],
                            ref["name"],
                            ref.get("namespace"),
                            bool(ref.get("optional")),
                        )
            elif key in VOLUME_REFERENCE_KEYS and isinstance(value, dict):
                kind, name_key = VOLUME_REFERENCE_KEYS[key]
                if value.get(name_key):
                    yield here, kind, value[name_key], None, bool(value.get("optional"))
            else:
                yield from iter_references(value, here)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from iter_references(item, f"{path}[{index}]")


def check_dangling_references(
    obj: dict, resolver: NameResolver, now: datetime, threshold: timedelta
) -> list[dict]:
    spec = obj.get("spec")
    if not isinstance(spec, dict):
        return []
    written = spec_write_time(obj)
    if written is None:
        return []
    age = now - written
    if age < threshold:
        return []
    own_namespace = (obj.get("metadata") or {}).get("namespace", "")
    findings = []
    seen: set[tuple[str, str, str]] = set()
    for path, kind, name, namespace, optional in iter_references(spec):
        if optional:
            continue
        target_ns = namespace or own_namespace
        key = (kind, target_ns, name)
        if key in seen:
            continue
        seen.add(key)
        if resolver.exists(kind, target_ns, name) is False:
            where = f"{kind}/{name}" if target_ns == own_namespace else f"{target_ns}/{kind}/{name}"
            detail = f"{path} -> {where} not found"
            findings.append(finding(obj, HEURISTIC_REFERENCE, detail, age))
    return findings


def analyze(
    objects: list[dict],
    events: list[dict],
    resolver: NameResolver,
    now: datetime,
    override_minutes: int | None = None,
) -> list[dict]:
    """Apply every heuristic to every object; pure apart from the resolver."""
    events_by_object = index_events(events)
    findings: list[dict] = []
    for obj in objects:
        kind = obj.get("kind", "")
        if kind in SKIPPED_OBJECT_KINDS:
            continue
        name = (obj.get("metadata") or {}).get("name", "")
        threshold = threshold_for(kind, override_minutes)
        findings.extend(check_generation_lag(obj, now, threshold))
        findings.extend(check_stale_conditions(obj, now, threshold))
        findings.extend(
            check_repeating_warnings(obj, events_by_object.get((kind, name), []), now, threshold)
        )
        findings.extend(check_dangling_references(obj, resolver, now, threshold))
    findings.sort(key=lambda f: (-f["stalled_seconds"], f["object"], f["heuristic"]))
    return findings


def stalled_object_count(findings: list[dict]) -> int:
    return len({(f["namespace"], f["object"]) for f in findings})


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------


def render_table(findings: list[dict]) -> str:
    rows = [[f["object"], f["heuristic"], f["detail"], f["stalled_for"]] for f in findings]
    widths = [len(col) for col in TABLE_COLUMNS]
    for row in rows:
        widths = [max(w, len(cell)) for w, cell in zip(widths, row)]
    lines = ["  ".join(col.ljust(w) for col, w in zip(TABLE_COLUMNS, widths)).rstrip()]
    for row in rows:
        lines.append("  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip())
    return "\n".join(lines)


def collect(namespace: str, kinds: str | None) -> tuple[list[dict], list[dict]]:
    resources = kinds.split(",") if kinds else namespaced_resources()
    if not resources:
        return [], []
    listing = kubectl_json(
        ["get", ",".join(resources), "-n", namespace, "--show-managed-fields"]
    )
    objects = listing.get("items") or []
    if not isinstance(objects, list):
        objects = [listing]
    events = kubectl_json(["get", "events", "-n", namespace]).get("items") or []
    return objects, events


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--namespace", "-n", required=True, help="namespace to inspect")
    parser.add_argument(
        "--kind",
        help="comma-separated kubectl resource names to inspect instead of every "
        "namespaced kind (e.g. deployments,gateways.gateway.networking.k8s.io)",
    )
    parser.add_argument(
        "--threshold-minutes",
        type=int,
        help=f"stall age for every kind, replacing the default of "
        f"{DEFAULT_STALL_MINUTES} and the per-kind table {STALL_MINUTES_BY_KIND}",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args(argv)

    try:
        objects, events = collect(args.namespace, args.kind)
    except RuntimeError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return EXIT_ERROR

    findings = analyze(
        objects, events, NameResolver(), datetime.now(timezone.utc), args.threshold_minutes
    )
    count = stalled_object_count(findings)
    if args.json:
        print(
            json.dumps(
                {"namespace": args.namespace, "stalled_resources": count, "findings": findings},
                indent=2,
            )
        )
    else:
        print(render_table(findings))
        print(SUMMARY_LINE.format(count=count))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
