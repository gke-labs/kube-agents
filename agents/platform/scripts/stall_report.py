#!/usr/bin/env python3
"""stall_report.py -- find controllers that stopped making progress without erroring.

Reads one namespace (or one kind in it) through ``kubectl get -o json``, one
kind per call, and applies four heuristics, each gated by an age threshold so that a controller
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
# The kubectl vocabulary this script uses. --show-managed-fields matters: kubectl
# strips managedFields from -o json by default, and the generation-lag and
# dangling-reference ages are read from them.
KUBECTL_GET = "get"
KUBECTL_API_RESOURCES_ARGS = ("api-resources", "--namespaced=true", "--verbs=list")
NAMESPACE_FLAG = "-n"
OUTPUT_FLAG = "-o"
OUTPUT_JSON = "json"
OUTPUT_NAME = "name"
SHOW_MANAGED_FIELDS_FLAG = "--show-managed-fields"
FIELD_SELECTOR_FLAG = "--field-selector"
EVENTS_RESOURCE = "events"
# kubectl accepts `kind/name`; a --kind carrying one returns a single object
# rather than a List.
RESOURCE_NAME_SEPARATOR = "/"
WARNING_PREFIX = "warning: "

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
# The same holds for anything else that has finished: a Job whose Complete or
# Failed condition is True, and any object whose spec asks for zero replicas.
# A Deployment keeps up to ten retired ReplicaSets at replicas: 0, and once a
# hash-suffixed ConfigMap generator has pruned the old names each one's envFrom
# resolves to nothing; a CronJob's history Jobs do the same once their Secret
# is renamed. Neither is a stall, so neither is read by any heuristic.
FINISHED_JOB_CONDITION_TYPES = frozenset({"Complete", "Failed"})
REPLICAS_SPEC_KEY = "replicas"
JOB_KIND = "Job"

# What counts as a repeating warning in one snapshot: at least this many
# occurrences, spanning at least the threshold, the newest inside the last
# threshold window. A single snapshot cannot watch a count rise; the span and
# the recency together are the closest a single read gets.
WARNING_EVENT_TYPE = "Warning"
REPEATING_EVENT_MIN_COUNT = 3
# Only Warning events feed the heuristic, so only they are fetched.
WARNING_EVENTS_SELECTOR = f"type={WARNING_EVENT_TYPE}"

# managedFields operations that write spec. "Update" also covers status writes
# by controllers, so an entry only counts when its fieldsV1 touches f:spec.
SPEC_WRITE_OPERATIONS = frozenset({"Apply", "Update"})
SPEC_FIELD_KEY = "f:spec"
# A Deployment with spec.paused set is not progressing on purpose, so its
# progress conditions are not read.
PAUSED_SPEC_KEY = "paused"
# How a list item under status is labelled in a condition row: its own name
# (Gateway listeners) or the name of the parent it reports on (HTTPRoute
# status.parents[].parentRef), falling back to its index.
LIST_ITEM_NAME_KEY = "name"
LIST_ITEM_PARENT_REF_KEY = "parentRef"

# Resources a whole-namespace scan skips. Events are read separately for the
# repeating-warnings heuristic; Secrets are never fetched as objects (their
# names come from `-o name` when a reference needs resolving); metrics are
# samples, not reconciled objects. Endpoints carry no conditions and kubectl
# prints a deprecation warning for every read of them. ConfigMaps,
# ControllerRevisions, EndpointSlices and Leases carry no conditions and no
# spec references, so no heuristic can report one, and they are the bulk of a
# busy namespace: ConfigMap bodies, a pod template per revision, an endpoint
# per Pod.
SCAN_EXCLUDED_RESOURCES = frozenset(
    {
        "events",
        "events.events.k8s.io",
        "secrets",
        "endpoints",
        "configmaps",
        "controllerrevisions.apps",
        "endpointslices.discovery.k8s.io",
        "leases.coordination.k8s.io",
    }
)
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
# to a kind not listed here is left alone rather than reported missing, and so
# is a kind the identity cannot list: on the default read-only permission set
# the Cluster Agent cannot list Secrets, so Secret-typed references go
# unchecked there and a warning says so.
REFERENT_RESOURCES: dict[str, str] = {
    "Secret": "secrets",
    "ConfigMap": "configmaps",
    "Service": "services",
    "Gateway": "gateways.gateway.networking.k8s.io",
    "PersistentVolumeClaim": "persistentvolumeclaims",
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
# The table's DETAIL column is capped, with a marker, so one event message
# cannot push a row off the screen; the finding dict behind it, and so --json,
# keeps the whole string. The cap is sized for the message that names the
# referent last: the GKE Gateway controller's SYNC event carries the namespace
# twice and the Gateway and Secret names once each, about 330 characters when
# all four run to the 63-character maximum.
TABLE_DETAIL_MAX_CHARS = 400
TRUNCATION_MARKER = "..."
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400

ZULU_SUFFIX = "Z"
UTC_OFFSET = "+00:00"
UNKNOWN_LABEL = "?"

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


def warn_lines(stderr: str) -> None:
    for line in stderr.splitlines():
        if line.strip():
            sys.stderr.write(f"{WARNING_PREFIX}{line}\n")


def kubectl_json(args: list[str]) -> dict:
    """Run `kubectl ... -o json` and parse it.

    kubectl asked for several resource types at once still prints the ones it
    could read when one of them fails, exiting non-zero. Parseable output wins
    and the failure is a warning; only unparseable output is an error.
    """
    rc, stdout, stderr = run_kubectl([*args, OUTPUT_FLAG, OUTPUT_JSON])
    warn_lines(stderr)
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
    rc, stdout, stderr = run_kubectl(
        [KUBECTL_GET, resource, NAMESPACE_FLAG, namespace, OUTPUT_FLAG, OUTPUT_NAME]
    )
    if rc != 0:
        sys.stderr.write(
            f"{WARNING_PREFIX}cannot list {resource} in {namespace}; references to "
            f"{resource} are not checked: {stderr.strip()}\n"
        )
        return None
    return {
        line.split(RESOURCE_NAME_SEPARATOR, 1)[-1] for line in stdout.splitlines() if line.strip()
    }


def namespaced_resources() -> list[str]:
    """Every listable namespaced resource, minus the scan exclusions.

    kubectl api-resources prints the full list and still exits non-zero when
    one aggregated API is unavailable; that is a warning, and only an empty
    listing is an error.
    """
    rc, stdout, stderr = run_kubectl([*KUBECTL_API_RESOURCES_ARGS, OUTPUT_FLAG, OUTPUT_NAME])
    warn_lines(stderr)
    if not stdout.strip():
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
    text = value[:-1] + UTC_OFFSET if value.endswith(ZULU_SUFFIX) else value
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
    kind = obj.get("kind", UNKNOWN_LABEL)
    name = (obj.get("metadata") or {}).get("name", UNKNOWN_LABEL)
    return f"{kind}{RESOURCE_NAME_SEPARATOR}{name}"


def finding(obj: dict, heuristic: str, detail: str, stalled: timedelta) -> dict:
    seconds = int(stalled.total_seconds())
    return {
        "object": object_label(obj),
        "namespace": (obj.get("metadata") or {}).get("namespace", ""),
        "heuristic": heuristic,
        "detail": detail,
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
            yield from iter_conditions(item, f"{path}[{list_item_label(item, index)}]")


def list_item_label(item, index: int) -> str:
    if isinstance(item, dict):
        parent = item.get(LIST_ITEM_PARENT_REF_KEY)
        name = item.get(LIST_ITEM_NAME_KEY) or (
            parent.get(LIST_ITEM_NAME_KEY) if isinstance(parent, dict) else None
        )
        if name:
            return str(name)
    return str(index)


def check_stale_conditions(obj: dict, now: datetime, threshold: timedelta) -> list[dict]:
    status = obj.get("status")
    if not isinstance(status, dict):
        return []
    spec = obj.get("spec")
    if isinstance(spec, dict) and spec.get(PAUSED_SPEC_KEY):
        return []
    # A condition cannot have transitioned before the object existed. Gateway
    # API objects are born with Accepted/Programmed=Unknown conditions stamped
    # with the Unix epoch, so an unclamped age would read as decades.
    created = parse_time((obj.get("metadata") or {}).get("creationTimestamp"))
    findings = []
    for path, cond in iter_conditions(status):
        ctype = cond.get("type")
        if ctype not in PROGRESS_CONDITION_TYPES or cond.get("status") == CONDITION_TRUE:
            continue
        since = parse_time(cond.get("lastTransitionTime"))
        if since is None:
            continue
        if created is not None and since < created:
            since = created
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
        involved = event.get("involvedObject") or {}
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


def is_terminal_pod(obj: dict) -> bool:
    status = obj.get("status")
    return (
        obj.get("kind") == "Pod"
        and isinstance(status, dict)
        and status.get("phase") in TERMINAL_POD_PHASES
    )


def is_finished(obj: dict) -> bool:
    """A Job that has completed or failed, or anything scaled to zero."""
    spec = obj.get("spec")
    if obj.get("kind") == JOB_KIND:
        status = obj.get("status")
        conditions = status.get(CONDITIONS_KEY) if isinstance(status, dict) else None
        return any(
            isinstance(cond, dict)
            and cond.get("type") in FINISHED_JOB_CONDITION_TYPES
            and cond.get("status") == CONDITION_TRUE
            for cond in conditions or []
        )
    return isinstance(spec, dict) and spec.get(REPLICAS_SPEC_KEY) == 0


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
        if kind in SKIPPED_OBJECT_KINDS or is_terminal_pod(obj) or is_finished(obj):
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


def table_detail(detail: str) -> str:
    if len(detail) <= TABLE_DETAIL_MAX_CHARS:
        return detail
    return detail[: TABLE_DETAIL_MAX_CHARS - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


def render_table(findings: list[dict]) -> str:
    rows = [
        [f["object"], f["heuristic"], table_detail(f["detail"]), f["stalled_for"]]
        for f in findings
    ]
    widths = [len(col) for col in TABLE_COLUMNS]
    for row in rows:
        widths = [max(w, len(cell)) for w, cell in zip(widths, row)]
    lines = ["  ".join(col.ljust(w) for col, w in zip(TABLE_COLUMNS, widths)).rstrip()]
    for row in rows:
        lines.append("  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip())
    return "\n".join(lines)


def collect(namespace: str, kinds: str | None) -> tuple[list[dict], list[dict]]:
    """Objects of every requested kind in the namespace, and its Warning events.

    One kubectl call per kind, not one for the namespace. In the Cluster
    Agent's shell, kubectl is the credential-proxy shim: the broker cuts a
    reply at its output cap (CREDENTIAL_PROXY_MAX_OUTPUT_BYTES, 4 MiB by
    default) and hands back the prefix with kubectl's own exit code, so a busy
    namespace read in one call arrives as JSON cut mid-string. Read per kind,
    an overflow costs that one kind, and a warning names it; only a scan that
    could read no kind at all is an error.
    """
    resources = [r for r in (kinds.split(",") if kinds else namespaced_resources()) if r]
    if not resources:
        return [], []
    objects: list[dict] = []
    unread: list[str] = []
    for resource in resources:
        try:
            listing = kubectl_json(
                [KUBECTL_GET, resource, NAMESPACE_FLAG, namespace, SHOW_MANAGED_FIELDS_FLAG]
            )
        except RuntimeError as exc:
            unread.append(resource)
            sys.stderr.write(
                f"{WARNING_PREFIX}{resource} in {namespace} not scanned; its objects are "
                f"missing from the count: {exc}\n"
            )
            continue
        # A `kind/name` in --kind returns the one object rather than a List.
        objects.extend((listing.get("items") or []) if "items" in listing else [listing])
    if len(unread) == len(resources):
        raise RuntimeError(f"no kind could be read in {namespace}: {', '.join(unread)}")
    try:
        events = kubectl_json(
            [
                KUBECTL_GET,
                EVENTS_RESOURCE,
                NAMESPACE_FLAG,
                namespace,
                FIELD_SELECTOR_FLAG,
                WARNING_EVENTS_SELECTOR,
            ]
        ).get("items")
    except RuntimeError as exc:
        sys.stderr.write(
            f"{WARNING_PREFIX}events in {namespace} not read; {HEURISTIC_EVENTS} is not "
            f"checked: {exc}\n"
        )
        events = []
    return objects, list(events or [])


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
