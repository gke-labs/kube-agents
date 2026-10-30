#!/usr/bin/env python3
"""A copy-pasteable, identifier-free usage and feedback summary of one install (issue #3428).

Customers in restricted networks cannot send telemetry. What they can do is paste
a short block of text into an email or a chat message after reading it. This
module assembles that block from three sources and nothing else:

* the ``PlatformAgent`` resource, read with ``kubectl get`` (required): mode,
  image tag, readiness, install age and ``status.usage``;
* the gateway pod, through one ``kubectl exec`` for each (optional): the session-kv
  task feed on its loopback port, rolled up to counts and durations, and the
  inference gateway's Prometheus text for token, spend and latency figures;
* ``kubectl version`` and the gateway's ConfigMap for the Kubernetes version and
  the model name (optional).

The block carries static enums, version strings, integer counters and durations.
No cluster name, project id, namespace, task title, summary, result or error text
reaches it: the task roll-up reads status and timestamps only, and ``main`` refuses
to print a block in which any identifier read from the resource appears. Every
optional source that fails prints as ``unavailable``; the block never exceeds
SUMMARY_MAX_LINES so a reader can check all of it before pasting.

Run from a workstation::

    python3 agents/platform/scripts/feedback_summary.py --namespace kubeagents-system

No model is in the loop: the output is a deterministic function of the sources.
"""

from __future__ import annotations

import argparse
import dataclasses
import functools
import datetime as dt
import json
import math
import re
import subprocess
import sys
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, TextIO, Tuple

SUMMARY_MAX_LINES = 40
SUMMARY_TITLE = "kube-agents usage & feedback summary"
TASK_WINDOW_DAYS = 14
RECENT_TASKS_SHOWN = 5
TASK_FEED_LIMIT = 200
DEFAULT_NAMESPACE = "kubeagents-system"
DEFAULT_AGENT_NAME = "platform-agent"
GATEWAY_POD_INFIX = "-gateway-"
POD_PHASE_RUNNING = "Running"
AGENT_CONTAINER = "platform-agent"
SESSION_KV_URL = "http://127.0.0.1:8699/v1/tasks"
SESSION_KV_KEY_ENV = "SESSION_KV_API_KEY"
INFERENCE_GATEWAY_METRICS_URL = "http://inference-gateway/metrics/"
#: The gateway's ConfigMap: this exact name under the chart, this name plus a
#: content hash under the kustomize base.
LITELLM_CONFIG_MAP = "litellm-config"
LITELLM_CONFIG_MAP_HASHED_PREFIX = LITELLM_CONFIG_MAP + "-"
LITELLM_CONFIG_KEY = "config.yaml"
LITELLM_DEPLOYMENT = "litellm"
KUBECTL = "kubectl"
KUBECTL_TIMEOUT_SECONDS = 30
CURL_TIMEOUT_SECONDS = 10
#: ``-f`` makes an HTTP error an exit code, so an error body is never read as data.
CURL_FLAGS = "-sSf"
CURL_FLAGS_FOLLOW = "-sSfL"
#: Why an optional source is missing, for the one stderr line.
REASON_NO_POD = "no running gateway pod"
REASON_EXEC = "the exec or the request failed"
REASON_SHAPE = "the answer had no tasks list"
REASON_SKIPPED = "skipped by --no-exec"
EXIT_OK = 0
EXIT_COLLECT = 1
EXIT_LEAK = 3
UNAVAILABLE = "unavailable"
NEVER = "never"
UNKNOWN = "unknown"
NONE = "none"
DEFAULT_MODE = "today"
READY_CONDITION = "Ready"
OTHER_BUCKET = "other"
SPEND_CURRENCY = "USD"
TRUNCATED_SUFFIX = "+"
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400
#: The two spec strings the block prints by design.
PRINTED_SPEC_PATHS: Tuple[Tuple[str, ...], ...] = (("mode",), ("deployment", "tag"))
#: Spec subtrees whose string leaves name the install (cluster, project, scope,
#: repositories, channels) and must not reach the block. ``deployment`` is left
#: out on purpose: its env values, annotations, selectors and sidecar args are
#: settings, not identifiers, and a value spelled "true" there would otherwise
#: collide with the block's own ``ready: true``.
IDENTIFIER_SPEC_SUBTREES: Tuple[str, ...] = ("harness", "scope", "integration", "security", "chat", "telemetry", "networkPolicy")
MIN_IDENTIFIER_LENGTH = 3
#: Spec strings that are settings in any subtree: booleans, numbers, enums the block prints.
IDENTIFIER_STOPWORDS = frozenset({"true", "false", "yes", "no", "on", "off", "none", "null"})
NUMERIC_VALUE = re.compile(r"^[0-9][0-9._%:-]*$")
IDENTIFIER_BOUNDARY = r"[A-Za-z0-9_.-]"
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_.-]+")
#: The instant the template is rendered at to learn its own vocabulary.
TEMPLATE_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
#: ``status.usage`` fields, in the order the block prints them; a missing one prints 0.
USAGE_COUNTERS: Tuple[str, ...] = (
    "clustersRegistered",
    "clustersMonitored",
    "chatMessagesInboundTotal",
    "sessionsTotal",
    "toolExecutionsTotal",
    "eventsIngestedTotal",
    "remediationsProposedTotal",
    "remediationsMergedTotal",
)
#: Kanban statuses folded into the roll-up's buckets; anything else is "other".
STATUS_BUCKETS: Mapping[str, str] = {
    "done": "done",
    "archived": "done",
    "failed": "failed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "blocked": "blocked",
    "running": "active",
    "ready": "active",
    "todo": "active",
    "triage": "active",
    "scheduled": "active",
    "review": "active",
}
BUCKET_ORDER: Tuple[str, ...] = ("done", "failed", "cancelled", "blocked", "active", OTHER_BUCKET)
#: Prometheus families the inference gateway may publish, by LiteLLM version.
TOKEN_FAMILIES: Mapping[str, Tuple[str, ...]] = {
    "in": ("litellm_input_tokens_metric_total", "litellm_input_tokens_metric", "litellm_input_tokens_total", "litellm_input_tokens"),
    "out": ("litellm_output_tokens_metric_total", "litellm_output_tokens_metric", "litellm_output_tokens_total", "litellm_output_tokens"),
}
SPEND_FAMILIES: Tuple[str, ...] = ("litellm_spend_metric_total", "litellm_spend_metric")
LATENCY_FAMILIES: Tuple[str, ...] = ("litellm_request_total_latency_metric", "litellm_llm_api_latency_metric")
LATENCY_QUANTILES: Tuple[float, float] = (0.5, 0.95)
#: Completion-time quantiles over the window's done tasks, nearest rank.
COMPLETION_QUANTILES: Tuple[float, float] = (0.5, 0.95)
#: Labels are read as quoted strings, so a ``}`` inside a value does not end them.
PROMETHEUS_LINE = re.compile(r'^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>(?:[^"}]|"(?:[^"\\]|\\.)*")*)\})?\s+(?P<value>\S+)')
PROMETHEUS_LABEL = re.compile(r'(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)="(?P<value>(?:[^"\\]|\\.)*)"')
LITELLM_MODEL_LINE = re.compile(r"^\s*model:\s*['\"]?(?P<model>[A-Za-z0-9_./:@+-]+)", re.MULTILINE)
#: A model given as a resource path (``vertex_ai/projects/<id>/locations/<region>/endpoints/<n>``)
#: names a project and a region between its first segment and its last; the block
#: prints the provider and the last segment only, so neither reaches it and the
#: audit does not refuse a block over a project the spec also names.
MODEL_SEGMENT_SEPARATOR = "/"
FEEDBACK_TEMPLATE: Tuple[str, ...] = (
    "feedback:",
    "  Rating: [ 👍 / 👎 / 😐 ]",
    "  What worked well:",
    "  What failed or was slow:",
)


class CollectError(RuntimeError):
    """The required source, the PlatformAgent resource, could not be read."""


@dataclasses.dataclass(frozen=True)
class Sample:
    name: str
    labels: Mapping[str, str]
    value: float


@dataclasses.dataclass(frozen=True)
class Options:
    namespace: str = DEFAULT_NAMESPACE
    name: str = DEFAULT_AGENT_NAME
    context: Optional[str] = None
    kubeconfig: Optional[str] = None
    in_pod: bool = True
    now: Optional[dt.datetime] = None


@dataclasses.dataclass(frozen=True)
class Snapshot:
    generated_at: dt.datetime
    cr: Mapping[str, Any]
    server_version: Optional[Mapping[str, Any]]
    litellm_config: Optional[str]
    tasks: Optional[Sequence[Mapping[str, Any]]]
    litellm_metrics: Optional[str]
    sources: Mapping[str, bool]
    tasks_truncated: bool = False
    #: Why a source in ``sources`` is False, by source name.
    source_reasons: Mapping[str, str] = dataclasses.field(default_factory=dict)

    @property
    def identifiers(self) -> List[str]:
        """Strings read from the resource that must never appear in the block.

        The resource's name and namespace, plus the string leaves of the spec
        subtrees that name the install (IDENTIFIER_SPEC_SUBTREES: cluster,
        project, location, scope entries, repositories, channels), skipping
        booleans, numbers, strings under MIN_IDENTIFIER_LENGTH and the
        template's own vocabulary. ``deployment`` is a settings block and is
        not read; nothing the block prints draws from it.
        """
        spec = self.cr.get("spec") or {}
        metadata = self.cr.get("metadata") or {}
        printed = {_spec_value(spec, path) for path in PRINTED_SPEC_PATHS}
        values = [metadata.get("name"), metadata.get("namespace")]
        for subtree in IDENTIFIER_SPEC_SUBTREES:
            values.extend(_string_leaves(spec.get(subtree)))
        seen: List[str] = []
        for value in values:
            if not isinstance(value, str) or len(value) < MIN_IDENTIFIER_LENGTH or value in printed or value in seen:
                continue
            if value.lower() in IDENTIFIER_STOPWORDS or NUMERIC_VALUE.match(value):
                continue
            seen.append(value)
        return seen


def _spec_value(spec: Mapping[str, Any], path: Sequence[str]) -> Any:
    value: Any = spec
    for key in path:
        value = value.get(key) if isinstance(value, Mapping) else None
    return value


def _string_leaves(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [leaf for item in value.values() for leaf in _string_leaves(item)]
    if isinstance(value, (list, tuple)):
        return [leaf for item in value for leaf in _string_leaves(item)]
    return []


# --- parsing -------------------------------------------------------------------

def parse_prometheus(text: str) -> List[Sample]:
    samples: List[Sample] = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = PROMETHEUS_LINE.match(line)
        if not match:
            continue
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        if not math.isfinite(value):
            continue
        labels = {m.group("key"): m.group("value") for m in PROMETHEUS_LABEL.finditer(match.group("labels") or "")}
        samples.append(Sample(match.group("name"), labels, value))
    return samples


def family_total(samples: Iterable[Sample], names: Sequence[str]) -> Optional[float]:
    """The sum over every series of the first family in ``names`` that has samples."""
    by_name: Dict[str, float] = {}
    for sample in samples:
        if sample.name in names:
            by_name[sample.name] = by_name.get(sample.name, 0.0) + sample.value
    for name in names:
        if name in by_name:
            return by_name[name]
    return None


def histogram_quantiles(samples: Iterable[Sample], family: str, quantiles: Sequence[float]) -> Optional[Tuple[float, ...]]:
    """Quantiles of a Prometheus histogram, interpolated inside the bucket, over all series."""
    buckets: Dict[float, float] = {}
    for sample in samples:
        if sample.name == f"{family}_bucket" and "le" in sample.labels:
            try:
                le = float(sample.labels["le"])
            except ValueError:
                continue
            buckets[le] = buckets.get(le, 0.0) + sample.value
    if not buckets:
        return None
    bounds = sorted(buckets)
    total = buckets[bounds[-1]]
    if total <= 0:
        return None
    results = []
    for quantile in quantiles:
        rank = quantile * total
        previous_bound, previous_count = 0.0, 0.0
        value = bounds[-1]
        for bound in bounds:
            count = buckets[bound]
            if count >= rank:
                if bound == float("inf"):
                    value = previous_bound
                else:
                    width = count - previous_count
                    value = previous_bound + (bound - previous_bound) * ((rank - previous_count) / width if width else 0.0)
                break
            previous_bound, previous_count = bound, count
        results.append(value)
    return tuple(results)


def model_from_litellm_config(config_yaml: Optional[str]) -> Optional[str]:
    """The first provider-qualified model the gateway routes to, as ``provider/name``.

    A resource path keeps its provider and its last segment only: the segments
    between them name a project and a region.
    """
    if not config_yaml:
        return None
    for match in LITELLM_MODEL_LINE.finditer(config_yaml):
        model = match.group("model")
        if MODEL_SEGMENT_SEPARATOR in model:
            provider, _, path = model.partition(MODEL_SEGMENT_SEPARATOR)
            return f"{provider}{MODEL_SEGMENT_SEPARATOR}{path.rsplit(MODEL_SEGMENT_SEPARATOR, 1)[-1]}"
    return None


# --- the roll-up ----------------------------------------------------------------

def _as_epoch(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def summarise_tasks(tasks: Sequence[Mapping[str, Any]], now: dt.datetime) -> Dict[str, Any]:
    """Counts by bucket, completion quantiles and the newest completed rows, without any text."""
    cutoff = now.timestamp() - TASK_WINDOW_DAYS * SECONDS_PER_DAY
    buckets: Dict[str, int] = {bucket: 0 for bucket in BUCKET_ORDER}
    durations: List[float] = []
    recent: List[Tuple[float, str, float]] = []
    for task in tasks:
        created = _as_epoch(task.get("created_at"))
        completed = _as_epoch(task.get("completed_at"))
        latest = completed or _as_epoch(task.get("updated_at")) or created
        if latest is None or latest < cutoff:
            continue
        status = str(task.get("status") or "")
        bucket = STATUS_BUCKETS.get(status, OTHER_BUCKET)
        buckets[bucket] += 1
        if completed is not None and created is not None:
            duration = max(0.0, completed - created)
            if bucket == "done":
                durations.append(duration)
            recent.append((now.timestamp() - created, status, duration))
    recent.sort(key=lambda row: row[0])
    quantiles = None
    if durations:
        ordered = sorted(durations)
        quantiles = tuple(_nearest_rank(ordered, q) for q in COMPLETION_QUANTILES)
    return {"total": sum(buckets.values()), "buckets": buckets, "quantiles": quantiles, "recent": recent[:RECENT_TASKS_SHOWN]}


def _nearest_rank(ordered: Sequence[float], quantile: float) -> float:
    return ordered[min(len(ordered) - 1, int(round(quantile * (len(ordered) - 1))))]


@functools.lru_cache(maxsize=1)
def template_tokens() -> frozenset:
    """Every token the block prints with no source at all: the template's own vocabulary."""
    empty = Snapshot(generated_at=TEMPLATE_EPOCH, cr={}, server_version=None, litellm_config=None,
                     tasks=None, litellm_metrics=None, sources={})
    return frozenset(TOKEN_PATTERN.findall(render(empty)))


def audit(text: str, identifiers: Iterable[str]) -> List[str]:
    """The identifiers that appear in ``text`` as whole tokens; an empty list means the block is clean.

    Whole tokens, not substrings, and never a word the template prints on its
    own: a namespace called ``kube-agents`` is not a leak because the title says
    ``kube-agents``, and a cluster called ``today`` is not one because the mode
    line can. Such an identifier cannot be told apart from the template, so the
    audit does not try; everything else that the resource declared is refused.
    """
    leaked = []
    for identifier in identifiers:
        if not identifier or identifier in template_tokens():
            continue
        pattern = rf"(?<!{IDENTIFIER_BOUNDARY}){re.escape(identifier)}(?!{IDENTIFIER_BOUNDARY})"
        if re.search(pattern, text):
            leaked.append(identifier)
    return leaked


# --- rendering ------------------------------------------------------------------

def _duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < SECONDS_PER_MINUTE:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, SECONDS_PER_MINUTE)
    if minutes < SECONDS_PER_MINUTE:
        return f"{minutes}m{rest:02d}s" if rest else f"{minutes}m"
    hours, minutes = divmod(minutes, SECONDS_PER_MINUTE)
    return f"{hours}h{minutes:02d}m"


def _age(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < SECONDS_PER_HOUR:
        return f"{max(1, seconds // SECONDS_PER_MINUTE)}m ago"
    if seconds < SECONDS_PER_DAY:
        return f"{seconds // SECONDS_PER_HOUR}h ago"
    return f"{seconds // SECONDS_PER_DAY}d ago"


def _install_age(cr: Mapping[str, Any], now: dt.datetime) -> str:
    created = _as_epoch((cr.get("metadata") or {}).get("creationTimestamp"))
    if created is None:
        return UNKNOWN
    return f"{int((now.timestamp() - created) // SECONDS_PER_DAY)} days"


def _ready(cr: Mapping[str, Any]) -> str:
    for condition in (cr.get("status") or {}).get("conditions") or []:
        if condition.get("type") == READY_CONDITION:
            return str(condition.get("status", UNKNOWN)).lower()
    return UNKNOWN


def _server_version(server_version: Optional[Mapping[str, Any]]) -> str:
    if not server_version:
        return UNAVAILABLE
    return str((server_version.get("serverVersion") or {}).get("gitVersion") or UNAVAILABLE)


def render(snapshot: Snapshot) -> str:
    cr, now = snapshot.cr, snapshot.generated_at
    spec, status = cr.get("spec") or {}, cr.get("status") or {}
    usage = status.get("usage") or {}
    lines: List[str] = ["```", f"{SUMMARY_TITLE} ({now.strftime('%Y-%m-%d')})", "environment:"]
    lines.append(f"  version: {(spec.get('deployment') or {}).get('tag') or UNKNOWN}")
    lines.append(f"  mode: {spec.get('mode') or DEFAULT_MODE}")
    lines.append(f"  model: {model_from_litellm_config(snapshot.litellm_config) or UNAVAILABLE}")
    lines.append(f"  kubernetes: {_server_version(snapshot.server_version)}")
    lines.append(f"  installed: {_install_age(cr, now)}; ready: {_ready(cr)}")
    interfaces = usage.get("activeInterfaces") or []
    lines.append(f"  interfaces: {', '.join(sorted(str(i) for i in interfaces)) or NONE}")
    lines.append("usage:")
    for counter in USAGE_COUNTERS:
        lines.append(f"  {counter}: {int(usage.get(counter) or 0)}")
    lines.append(f"  lastActive: {usage.get('lastActiveTime') or NEVER}")
    lines.append("performance:")
    if snapshot.litellm_metrics is not None:
        samples = parse_prometheus(snapshot.litellm_metrics)
        tokens_in = family_total(samples, TOKEN_FAMILIES["in"])
        tokens_out = family_total(samples, TOKEN_FAMILIES["out"])
        spend = family_total(samples, SPEND_FAMILIES)
        latency = next((q for q in (histogram_quantiles(samples, f, LATENCY_QUANTILES) for f in LATENCY_FAMILIES) if q), None)
    else:
        tokens_in = tokens_out = spend = latency = None
    tokens = f"in {int(tokens_in)} / out {int(tokens_out)}" if tokens_in is not None and tokens_out is not None else UNAVAILABLE
    lines.append(f"  tokens: {tokens}")
    lines.append(f"  spend: {spend:.2f} {SPEND_CURRENCY}" if spend is not None else f"  spend: {UNAVAILABLE}")
    lines.append(f"  latency p50 {latency[0]:.1f}s / p95 {latency[1]:.1f}s" if latency else f"  latency: {UNAVAILABLE}")
    if snapshot.tasks is None:
        lines.append(f"tasks ({TASK_WINDOW_DAYS} days): {UNAVAILABLE}")
    else:
        rollup = summarise_tasks(snapshot.tasks, now)
        counts = ", ".join(f"{bucket} {rollup['buckets'][bucket]}" for bucket in BUCKET_ORDER if rollup["buckets"][bucket])
        total = f"{rollup['total']}{TRUNCATED_SUFFIX if snapshot.tasks_truncated else ''}"
        lines.append(f"tasks ({TASK_WINDOW_DAYS} days): {total}" + (f" ({counts})" if counts else ""))
        if rollup["quantiles"]:
            lines.append(f"  completed p50 {_duration(rollup['quantiles'][0])} / p95 {_duration(rollup['quantiles'][1])}")
        for age, status_name, duration in rollup["recent"]:
            lines.append(f"  - {_age(age):<8} {status_name:<8} {_duration(duration)}")
    lines.extend(FEEDBACK_TEMPLATE)
    lines.append("```")
    return "\n".join(lines)


# --- collection -----------------------------------------------------------------

def _kubectl(options: Options, *args: str, timeout: int = KUBECTL_TIMEOUT_SECONDS) -> str:
    command = [KUBECTL]
    if options.context:
        command += ["--context", options.context]
    if options.kubeconfig:
        command += ["--kubeconfig", options.kubeconfig]
    command += list(args)
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode != 0:
        raise CollectError(f"{' '.join(command)}: {result.stderr.strip() or 'exit ' + str(result.returncode)}")
    return result.stdout


def _optional(options: Options, *args: str, timeout: int = KUBECTL_TIMEOUT_SECONDS) -> Optional[str]:
    try:
        return _kubectl(options, *args, timeout=timeout)
    except (CollectError, OSError, subprocess.SubprocessError):
        return None


def _gateway_pod(options: Options) -> Optional[str]:
    raw = _optional(options, "get", "pods", "-n", options.namespace, "-o", "json")
    if raw is None:
        return None
    try:
        items = json.loads(raw).get("items") or []
    except ValueError:
        return None
    for item in items:
        metadata = item.get("metadata") or {}
        name = metadata.get("name") or ""
        running = (item.get("status") or {}).get("phase") == POD_PHASE_RUNNING
        if name.startswith(options.name + GATEWAY_POD_INFIX) and running and not metadata.get("deletionTimestamp"):
            return name
    return None


def _in_pod_curl(options: Options, pod: str, shell: str) -> Optional[str]:
    return _optional(
        options, "exec", "-n", options.namespace, pod, "-c", AGENT_CONTAINER, "--", "sh", "-c", shell,
        timeout=KUBECTL_TIMEOUT_SECONDS + CURL_TIMEOUT_SECONDS,
    )


def _mounted_config_maps(options: Options) -> List[str]:
    """The ConfigMaps the gateway Deployment mounts: the live config under a kustomize hash."""
    raw = _optional(options, "get", "deployment", LITELLM_DEPLOYMENT, "-n", options.namespace, "-o", "json")
    if raw is None:
        return []
    try:
        volumes = (((json.loads(raw).get("spec") or {}).get("template") or {}).get("spec") or {}).get("volumes") or []
    except ValueError:
        return []
    return [v["configMap"]["name"] for v in volumes if isinstance(v.get("configMap"), dict) and v["configMap"].get("name")]


def _litellm_config(options: Options) -> Optional[str]:
    """The gateway's rendered config: the ConfigMap its Deployment mounts, else the newest of its names.

    The chart names it ``litellm-config``; the kustomize base appends a content
    hash and a re-apply leaves the older hashes behind, so the Deployment's own
    volume says which one is live, and creation time breaks a tie without it.
    """
    raw = _optional(options, "get", "configmap", "-n", options.namespace, "-o", "json")
    if raw is None:
        return None
    try:
        items = json.loads(raw).get("items") or []
    except ValueError:
        return None
    candidates = []
    for item in items:
        metadata = item.get("metadata") or {}
        name = metadata.get("name") or ""
        config = (item.get("data") or {}).get(LITELLM_CONFIG_KEY)
        if isinstance(config, str) and (name == LITELLM_CONFIG_MAP or name.startswith(LITELLM_CONFIG_MAP_HASHED_PREFIX)):
            candidates.append((name, str(metadata.get("creationTimestamp") or ""), config))
    if not candidates:
        return None
    mounted = set(_mounted_config_maps(options)) if len(candidates) > 1 else set()
    for name, _created, config in candidates:
        if name in mounted:
            return config
    return max(candidates, key=lambda c: c[1])[2]


def _task_feed(feed: str) -> Tuple[Optional[Sequence[Mapping[str, Any]]], bool]:
    """The feed's rows and its truncated flag, or ``(None, False)`` for anything not shaped like the feed.

    An object with a ``tasks`` list whose rows are objects is the feed; a
    non-object body, a non-list ``tasks`` or a row that is not an object is
    not, and reads as unavailable rather than as a traceback in ``render``.
    """
    try:
        body = json.loads(feed)
    except ValueError:
        return None, False
    if not isinstance(body, dict) or not isinstance(body.get("tasks"), list):
        return None, False
    rows = body["tasks"]
    if not all(isinstance(row, Mapping) for row in rows):
        return None, False
    return rows, bool(body.get("truncated"))


def collect(options: Options) -> Snapshot:
    now = options.now or dt.datetime.now(dt.timezone.utc)
    try:
        cr = json.loads(_kubectl(options, "get", "platformagent", options.name, "-n", options.namespace, "-o", "json"))
    except (CollectError, OSError, subprocess.SubprocessError, ValueError) as error:
        raise CollectError(f"cannot read platformagent {options.name} in {options.namespace}: {error}") from error
    server_version = None
    raw_version = _optional(options, "version", "-o", "json")
    if raw_version:
        try:
            server_version = json.loads(raw_version)
        except ValueError:
            server_version = None
    litellm_config = _litellm_config(options)
    tasks: Optional[Sequence[Mapping[str, Any]]] = None
    truncated = False
    metrics: Optional[str] = None
    reasons: Dict[str, str] = {}
    if not options.in_pod:
        reasons = {"session-kv": REASON_SKIPPED, "litellm-metrics": REASON_SKIPPED}
    else:
        pod = _gateway_pod(options)
        if not pod:
            reasons = {"session-kv": REASON_NO_POD, "litellm-metrics": REASON_NO_POD}
        else:
            since = int(now.timestamp()) - TASK_WINDOW_DAYS * SECONDS_PER_DAY
            feed = _in_pod_curl(
                options, pod,
                f'curl {CURL_FLAGS} -m {CURL_TIMEOUT_SECONDS} -H "X-API-Key: ${SESSION_KV_KEY_ENV}" '
                f'"{SESSION_KV_URL}?since={since}&limit={TASK_FEED_LIMIT}"',
            )
            if feed is None:
                reasons["session-kv"] = REASON_EXEC
            else:
                tasks, truncated = _task_feed(feed)
                if tasks is None:
                    reasons["session-kv"] = REASON_SHAPE
            metrics = _in_pod_curl(options, pod, f"curl {CURL_FLAGS_FOLLOW} -m {CURL_TIMEOUT_SECONDS} {INFERENCE_GATEWAY_METRICS_URL}")
            if metrics is None or not metrics.strip():
                metrics = None
                reasons["litellm-metrics"] = REASON_EXEC
    return Snapshot(
        generated_at=now, cr=cr, server_version=server_version, litellm_config=litellm_config,
        tasks=tasks, litellm_metrics=metrics, tasks_truncated=truncated,
        sources={"session-kv": tasks is not None, "litellm-metrics": metrics is not None},
        source_reasons=reasons,
    )


def main(argv: Optional[Sequence[str]] = None, stdout: TextIO = sys.stdout, stderr: TextIO = sys.stderr) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--namespace", "-n", default=DEFAULT_NAMESPACE)
    parser.add_argument("--name", default=DEFAULT_AGENT_NAME, help="the PlatformAgent resource")
    parser.add_argument("--context")
    parser.add_argument("--kubeconfig")
    parser.add_argument("--no-exec", action="store_true", help="read the resource only; skip the in-pod sources")
    args = parser.parse_args(argv)
    options = Options(namespace=args.namespace, name=args.name, context=args.context, kubeconfig=args.kubeconfig, in_pod=not args.no_exec)
    try:
        snapshot = collect(options)
    except CollectError as error:
        print(f"feedback-summary: {error}", file=stderr)
        return EXIT_COLLECT
    text = render(snapshot)
    missing = [name for name, present in snapshot.sources.items() if not present]
    if missing and options.in_pod:
        detail = "; ".join(f"{name}: {snapshot.source_reasons.get(name, REASON_EXEC)}" for name in missing)
        print(f"feedback-summary: could not be read: {detail}", file=stderr)
    leaked = audit(text, snapshot.identifiers)
    if leaked:
        print(f"feedback-summary: refused to print: the block names {', '.join(leaked)}", file=stderr)
        return EXIT_LEAK
    print(text, file=stdout)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
