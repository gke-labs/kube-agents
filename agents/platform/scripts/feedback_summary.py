#!/usr/bin/env python3
"""A copy-pasteable, identifier-free usage and feedback summary of one install (issue #3428).

Customers in restricted networks cannot send telemetry. What they can do is paste
a short block of text into an email or a chat message after reading it. This
module assembles that block from four sources and nothing else:

* the ``PlatformAgent`` resource, read with ``kubectl get`` (required): mode,
  image tag, readiness, install age and ``status.usage``;
* the gateway pod, through one ``kubectl exec`` for each (optional): the session-kv
  task feed on its loopback port, rolled up to counts and durations, and the
  inference gateway's Prometheus text for token, spend and latency figures;
* the credential broker pod's Prometheus text on loopback port 8766 (through
  ``kubectl exec``; optional), rolled up to the top five ``tool/subcommand`` pairs;
  when that cannot be read (``--no-exec`` included) the resource's
  ``status.usage.topTools``, the operator's reading of the same five pairs,
  stands in, rendered the same way the chat gateway's ``/feedback summary``
  renders it;
* ``kubectl version`` and cluster object counts for the Kubernetes version and the
  cluster scale line (optional); the model name is the resource's ``status.inference``,
  and the gateway's ConfigMap whenever the status has no ``inference`` key (optional):
  under an operator older than the field, before this operator's first Ready write
  (so for a CR Degraded since it was created), and under a served CRD that prunes
  the field.

The block carries static enums, version strings, integer counters and durations.
No cluster name, project id, namespace, task title, summary, result or error text
reaches it: the task roll-up reads status and timestamps only, and ``main`` refuses
to print a block in which any identifier read from the resource appears outside
the ``model:`` line, which is printed by design. Every optional source that fails
prints as ``unavailable`` (the cluster ``scale`` line is omitted when unread); the
block never exceeds SUMMARY_MAX_LINES so a reader can check all of it before pasting.

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

# Independent of the gateway's usageSummaryMaxLines: this block has lines the
# gateway's lacks.
SUMMARY_MAX_LINES = 57
SUMMARY_TITLE = "kube-agents usage & feedback summary"
TASK_WINDOW_DAYS = 14
RECENT_TASKS_SHOWN = 5
TOP_TOOLS_SHOWN = 5
TASK_FEED_LIMIT = 200
DEFAULT_NAMESPACE = "kubeagents-system"
DEFAULT_AGENT_NAME = "platform-agent"
GATEWAY_POD_INFIX = "-gateway-"
BROKER_POD_INFIX = "-credential-proxy-"
BROKER_CONTAINER = "envoy-credential-proxy"
BROKER_METRICS_PORT = 8766
BROKER_METRICS_URL = f"http://127.0.0.1:{BROKER_METRICS_PORT}/metrics"
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
REASON_NO_BROKER_POD = "no running credential-proxy pod"
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
KCC_CRD_SUFFIX = ".cnrm.cloud.google.com"
SCALE_RESOURCES: Tuple[Tuple[str, str], ...] = (
    ("namespaces", "namespaces"),
    ("deployments", "deployments.apps"),
    ("pods", "pods"),
    ("nodes", "nodes"),
    ("crds", "customresourcedefinitions.apiextensions.k8s.io"),
)
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
#: ``status.usage`` fields, in the order the block prints them; a missing counter
#: prints 0, a missing reading (USAGE_READINGS) prints USAGE_NO_READING.
USAGE_COUNTERS: Tuple[str, ...] = (
    "clustersRegistered",
    "clustersMonitored",
    "chatMessagesInboundTotal",
    "sessionsTotal",
    "chatSessionsTotal",
    "toolExecutionsTotal",
    "toolExecutionsBlockedTotal",
    "brokerFailuresTotal",
    "eventsIngestedTotal",
    "chatTurnsErroredTotal",
    "backgroundTasksGaveUpTotal",
    "negativeFeedbackTotal",
    "remediationsProposedTotal",
    "remediationsMergedTotal",
    "proposalsAuthored",
    "remediationsOpen",
    "remediationsClosedUnmergedTotal",
    "proposalHumanTouches",
    "proposalsMergedUntouched",
    "peopleSeen7d",
    "peopleSeen30d",
)
USAGE_NO_READING = "no reading"
USAGE_FORGE_READING_SUFFIX = " (as the forge reports)"
USAGE_GATEWAY_READING_SUFFIX = " (as the gateway reports)"
#: The fields read off a source rather than counted, each to the suffix naming
#: that source: absent means no reading yet, which is not 0. The broker reads
#: the six off the forge; the operator reads the two people readings off the
#: A2A gateway.
USAGE_READINGS: Mapping[str, str] = {
    "remediationsMergedTotal": USAGE_FORGE_READING_SUFFIX,
    "proposalsAuthored": USAGE_FORGE_READING_SUFFIX,
    "remediationsOpen": USAGE_FORGE_READING_SUFFIX,
    "remediationsClosedUnmergedTotal": USAGE_FORGE_READING_SUFFIX,
    "proposalHumanTouches": USAGE_FORGE_READING_SUFFIX,
    "proposalsMergedUntouched": USAGE_FORGE_READING_SUFFIX,
    "peopleSeen7d": USAGE_GATEWAY_READING_SUFFIX,
    "peopleSeen30d": USAGE_GATEWAY_READING_SUFFIX,
}
#: Credential broker tool-invocation family and closed outcome order; the
#: order is also the status row's field names, and the gateway's
#: usageSummaryToolStatusOrder verbatim.
TOOL_INVOCATIONS_FAMILY = "kubeagents_tool_invocations_total"
TOOL_STATUS_ORDER: Tuple[str, ...] = ("success", "error", "blocked", "busy", "abandoned")
#: ``status.usage.topTools``: the operator's reading of the top five pairs, the
#: fallback for an unread broker, and its row fields; the gateway's
#: usageTopTools* names verbatim.
TOP_TOOLS_STATUS_KEY = "topTools"
TOP_TOOLS_TOOL_FIELD = "tool"
TOP_TOOLS_SUBCOMMAND_FIELD = "subcommand"
TOP_TOOLS_TOTAL_FIELD = "total"
TOOL_PAIR_SEPARATOR = "/"
TOP_TOOLS_LABEL = f"tools (top {TOP_TOOLS_SHOWN}, since broker start)"
#: Closed per-tool subcommand vocabularies, held equal to
#: ``credential_proxy._subcommand_vocabulary(tool)`` by ``GatewayCopyTest``.
BROKER_SUBCOMMANDS: Mapping[str, frozenset] = {
    "kubectl": frozenset({
        "annotate", "api-resources", "api-versions", "apply", "attach", "auth",
        "autoscale", "cluster-info", "config", "cordon", "cp", "create", "debug",
        "delete", "describe", "diff", "drain", "edit", "events", "exec", "explain",
        "expose", "get", "label", "logs", "patch", "port-forward", "proxy", "replace",
        "rollout", "run", "scale", "set", "taint", "top", "uncordon", "version", "wait",
    }),
    "gcloud": frozenset({
        "artifacts", "asset", "auth", "billing", "components", "compute", "config",
        "container", "iam", "info", "init", "logging", "monitoring", "projects",
        "resource-manager", "services", "version",
    }),
    "git": frozenset({
        "add", "am", "apply", "archive", "blame", "branch", "bundle", "cat-file",
        "checkout", "cherry-pick", "clean", "clone", "commit", "describe", "fetch",
        "init", "log", "ls-files", "ls-remote", "ls-tree", "merge", "merge-base", "mv", "pull", "push", "rebase", "remote",
        "reset", "restore", "rev-list", "rev-parse", "revert", "rm", "show",
        "sparse-checkout", "stash", "status", "submodule", "switch", "symbolic-ref",
        "tag", "update-ref", "version", "worktree",
    }),
    "gh": frozenset({
        "api", "auth", "browse", "gist", "issue", "label", "pr", "project", "release",
        "repo", "run", "search", "secret", "ssh-key", "status", "variable", "version",
        "workflow",
    }),
    "feedback": frozenset({"submit"}),
}
BROKER_TOOLS = frozenset(BROKER_SUBCOMMANDS) | {OTHER_BUCKET}
#: Every word the tools or scale lines may print from closed vocabularies, so an
#: install whose spec happens to use a generic word is not refused for the
#: template's own vocabulary.
TEMPLATE_EXTRA_TOKENS = (
    BROKER_TOOLS
    | frozenset(sub for subs in BROKER_SUBCOMMANDS.values() for sub in subs)
    | frozenset(TOOL_STATUS_ORDER)
    | {"tools", "top", "since", "broker", "start", "scale", "namespaces", "deployments", "pods", "nodes", "crds", "kcc", NONE, OTHER_BUCKET}
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
#: The environment lines, in the order the block prints them; the gateway's
#: usageSummaryEnvironmentKeys verbatim.
ENVIRONMENT_KEYS: Tuple[str, ...] = ("version", "mode", "model", "kubernetes", "installed", "interfaces")
#: ``status.inference``: the operator's reading of the install's default model.
INFERENCE_STATUS_KEY = "inference"
INFERENCE_PROVIDER_FIELD = "provider"
INFERENCE_MODEL_FIELD = "model"
#: The model line's start; the audit runs over the block without this one line.
MODEL_LINE_PREFIX = "  model: "
#: The model_list entry whose model is the install's default (the operator's agentModelName).
LITELLM_DEFAULT_MODEL_ALIAS = "model-default"
#: The ``model_name: model-default`` line, quoted or bare, at any indent.
LITELLM_DEFAULT_NAME_LINE = re.compile(
    rf"^[ \t]*(?:-[ \t]+)?model_name:[ \t]*(?P<q>['\"]?){re.escape(LITELLM_DEFAULT_MODEL_ALIAS)}(?P=q)[ \t]*(?:#[^\n]*)?$",
    re.MULTILINE,
)
#: The same line followed, within its entry (indented lines that open no new
#: list item), by ``litellm_params:`` and that block's ``model:``, quoted or
#: bare, captured possibly empty.
#:
#: Two valid YAML shapes are not read: an entry with ``litellm_params:`` before
#: ``model_name:``, and an entry with a nested ``- `` list line between the name
#: line and ``litellm_params:``. Each prints ``unavailable`` with no fallback. No
#: config the repository renders uses either: the chart's
#: ``kube-agents.litellmConfig``, the kustomize ``litellm/base`` and
#: ``overlays/anthropic`` configs, and the ``examples/litellm-*`` configs all put
#: the name line first. This path runs whenever the status has no
#: ``inference`` key: under an operator older than ``status.inference``, before
#: this operator's first Ready write (so for a CR Degraded since it was
#: created), and under a served CRD that prunes the field.
#:
#: A hand-edited config can hit further divergences from the operator's Go
#: parser: a flow-style entry (``- {model_name: model-default, litellm_params:
#: {model: gemini/x}}``) reads ``unavailable`` here and the pair in Go; a nested
#: mapping under ``litellm_params`` whose own ``model:`` key precedes the real
#: one reads the nested value here; a ``#`` inside an unquoted value
#: (``hosted_vllm/acme#v2``) is truncated at the ``#`` here and kept in Go; an
#: unquoted value with a space (``gemini/flash 2``) reads the text before the
#: space here and is malformed in Go; and two entries both named
#: ``model-default``, the first in params-first order, read the second here and
#: the first in Go.
LITELLM_DEFAULT_ENTRY_MODEL = re.compile(
    LITELLM_DEFAULT_NAME_LINE.pattern + r"\n"
    r"(?:(?:[ \t]+(?![ \t-])[^\n]*)?\n)*?"
    r"[ \t]+litellm_params:[ \t]*(?:#[^\n]*)?\n"
    r"(?:(?:[ \t]+(?![ \t-])(?!model:)[^\n]*)?\n)*?"
    r"[ \t]+model:[ \t]*(?P<mq>['\"]?)(?P<model>[^'\"\s#]*)(?P=mq)",
    re.MULTILINE,
)
#: A CRLF line end, normalised to LF before the LiteLLM regexes run: MULTILINE ``$``
#: and the ``\n`` the entry pattern spells match only before LF.
CRLF_LINE_END = "\r\n"
LF_LINE_END = "\n"
#: A LiteLLM model is ``<provider>/<name>``, split at the first separator.
MODEL_SEGMENT_SEPARATOR = "/"
#: A name that starts with this is a Vertex AI endpoint path
#: (``projects/<id>/locations/<region>/endpoints/<n>``): only its last segment is kept.
MODEL_RESOURCE_PATH_PREFIX = "projects/"
#: The shortest full path, ``projects/<p>/locations/<l>/<kind>/<id>``, in segments.
MODEL_RESOURCE_PATH_MIN_SEGMENTS = 6
#: Where a full path carries its ``locations`` segment.
MODEL_RESOURCE_PATH_LOCATIONS_INDEX = 2
MODEL_RESOURCE_PATH_LOCATIONS = "locations"
#: A name segment that starts with this is a Bedrock ARN: only its resource id is kept.
MODEL_ARN_PREFIX = "arn:"
MODEL_ARN_FIELD_SEPARATOR = ":"
#: A full ARN's field count: ``arn:partition:service:region:account:resource``.
MODEL_ARN_FIELDS = 6
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
    broker_metrics: Optional[str] = None
    scale: Optional[Mapping[str, int]] = None

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


def trim_model_name(name: str) -> str:
    """The name after the provider, with a resource reference cut to its resource id.

    A reference must have its full shape, or the name is malformed and ``""``
    comes back. A Vertex AI path needs ``projects/<p>/locations/<l>/<kind>/<id>``
    at least and keeps the text after its last ``/``. A name with a segment that
    starts with ``arn:`` needs six ``:`` fields from there
    (``arn:partition:service:region:account:resource``) with a non-empty resource
    part, and keeps the text after the resource part's last ``/``, or, without
    one, the text after its last ``:``. So no project id, region or account id is
    printed. Every other name is kept as typed.
    """
    segments = name.split(MODEL_SEGMENT_SEPARATOR)
    if name.startswith(MODEL_RESOURCE_PATH_PREFIX):
        if (
            len(segments) < MODEL_RESOURCE_PATH_MIN_SEGMENTS
            or segments[MODEL_RESOURCE_PATH_LOCATIONS_INDEX] != MODEL_RESOURCE_PATH_LOCATIONS
        ):
            return ""
        return segments[-1]
    for index, segment in enumerate(segments):
        if not segment.startswith(MODEL_ARN_PREFIX):
            continue
        arn = MODEL_SEGMENT_SEPARATOR.join(segments[index:])
        fields = arn.split(MODEL_ARN_FIELD_SEPARATOR, MODEL_ARN_FIELDS - 1)
        if len(fields) < MODEL_ARN_FIELDS or not fields[-1]:
            return ""
        resource = fields[-1]
        if MODEL_SEGMENT_SEPARATOR in resource:
            return resource.rsplit(MODEL_SEGMENT_SEPARATOR, 1)[-1]
        return resource.rsplit(MODEL_ARN_FIELD_SEPARATOR, 1)[-1]
    return name


def _valid_provider(provider: str) -> bool:
    """A provider slug: not empty, no whitespace, and not itself a bare ARN or Vertex AI path."""
    return (
        bool(provider)
        and not any(c.isspace() for c in provider)
        and not provider.startswith(MODEL_ARN_PREFIX)
        and MODEL_ARN_FIELD_SEPARATOR not in provider
        and provider != MODEL_RESOURCE_PATH_PREFIX.rstrip(MODEL_SEGMENT_SEPARATOR)
    )


def split_model(litellm_model: str) -> Optional[str]:
    """``provider/name`` with both sides non-empty after trimming, else None (malformed).

    Whitespace on either side is malformed: the line must stay one line.
    """
    if litellm_model.endswith(MODEL_SEGMENT_SEPARATOR):
        return None
    provider, found, name = litellm_model.partition(MODEL_SEGMENT_SEPARATOR)
    if not found or not _valid_provider(provider):
        return None
    name = trim_model_name(name)
    if not name or any(c.isspace() for c in name):
        return None
    return f"{provider}{MODEL_SEGMENT_SEPARATOR}{name}"


def model_from_litellm_config(config_yaml: Optional[str]) -> Optional[str]:
    """The install's default model as the gateway's config states it, as ``provider/name``.

    The operator's rule, ported: the entry named ``model-default`` answers when
    it exists, and a malformed model there is None with no fallback; only when
    no entry carries that name does the first well-formed provider-qualified
    ``model:`` line answer.
    """
    if not config_yaml:
        return None
    config_yaml = config_yaml.replace(CRLF_LINE_END, LF_LINE_END)
    if LITELLM_DEFAULT_NAME_LINE.search(config_yaml):
        entry = LITELLM_DEFAULT_ENTRY_MODEL.search(config_yaml)
        return split_model(entry.group("model")) if entry else None
    # The scan reads every ``model:`` line, while the operator's parser reads only
    # ``model_list[].litellm_params.model``: a ``model:`` key elsewhere in the
    # config can answer here and not in the operator. A CRLF config reads the
    # same as LF: the line ends are normalised above.
    for match in LITELLM_MODEL_LINE.finditer(config_yaml):
        model = split_model(match.group("model"))
        if model:
            return model
    return None


def model_from_status(status: Mapping[str, Any]) -> Optional[str]:
    """``provider/model`` from ``status.inference``; UNAVAILABLE when the key is
    present without both fields (the operator's definite negative); None when the
    key is absent (an operator older than the field, no Ready write yet, or a
    served CRD that prunes it)."""
    if INFERENCE_STATUS_KEY not in status:
        return None
    inference = status.get(INFERENCE_STATUS_KEY)
    if not isinstance(inference, Mapping):
        return UNAVAILABLE
    provider, model = inference.get(INFERENCE_PROVIDER_FIELD), inference.get(INFERENCE_MODEL_FIELD)
    if isinstance(provider, str) and isinstance(model, str) and provider and model:
        return f"{provider}{MODEL_SEGMENT_SEPARATOR}{model}"
    return UNAVAILABLE


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


def _tool_pair(tool: str, sub: str) -> str:
    """The pair as the block prints it: the tool alone for a bare tool, else ``tool/sub``."""
    return tool if sub == NONE else f"{tool}{TOOL_PAIR_SEPARATOR}{sub}"


def summarise_broker_tools(samples: Iterable[Sample]) -> List[Tuple[str, int, Mapping[str, int]]]:
    """Top ``TOP_TOOLS_SHOWN`` tool/subcommand pairs by invocation count, with closed labels only.

    Sorted by total descending, then tool and subcommand ascending, the order
    the operator writes ``status.usage.topTools`` in, so the block reads the
    same from either source.
    """
    by_pair: Dict[Tuple[str, str], Dict[str, int]] = {}
    for sample in samples:
        if sample.name != TOOL_INVOCATIONS_FAMILY:
            continue
        count = int(round(sample.value))
        if count <= 0:
            continue
        raw_tool = (sample.labels.get(TOP_TOOLS_TOOL_FIELD) or OTHER_BUCKET).lower()
        raw_sub = (sample.labels.get(TOP_TOOLS_SUBCOMMAND_FIELD) or NONE).lower()
        raw_status = (sample.labels.get("status") or OTHER_BUCKET).lower()
        tool = raw_tool if raw_tool in BROKER_TOOLS else OTHER_BUCKET
        allowed_subs = BROKER_SUBCOMMANDS.get(tool, frozenset())
        if raw_sub == NONE:
            sub = NONE
        elif raw_sub in allowed_subs:
            sub = raw_sub
        else:
            sub = OTHER_BUCKET
        status = raw_status if raw_status in TOOL_STATUS_ORDER else OTHER_BUCKET
        bucket = by_pair.setdefault((tool, sub), {})
        bucket[status] = bucket.get(status, 0) + count
    rows = [(tool, sub, sum(counts.values()), counts) for (tool, sub), counts in by_pair.items()]
    rows.sort(key=lambda r: (-r[2], r[0], r[1]))
    return [(_tool_pair(tool, sub), total, counts) for tool, sub, total, counts in rows[:TOP_TOOLS_SHOWN]]


def summarise_status_tools(rows: Sequence[Any]) -> List[Tuple[str, int, Mapping[str, int]]]:
    """``status.usage.topTools`` in the shape ``summarise_broker_tools`` returns.

    The operator sorted and capped the list, so the rows are taken in their
    order, at most ``TOP_TOOLS_SHOWN`` of them; a row that is not an object is
    skipped. The gateway's ``renderTopTools`` reads the list the same way.
    """
    out: List[Tuple[str, int, Mapping[str, int]]] = []
    for row in rows:
        if not isinstance(row, Mapping) or len(out) >= TOP_TOOLS_SHOWN:
            continue
        tool = str(row.get(TOP_TOOLS_TOOL_FIELD) or "")
        sub = str(row.get(TOP_TOOLS_SUBCOMMAND_FIELD) or "")
        counts = {status: _count(row.get(status)) for status in TOOL_STATUS_ORDER if _count(row.get(status)) > 0}
        out.append((_tool_pair(tool, sub), _count(row.get(TOP_TOOLS_TOTAL_FIELD)), counts))
    return out


def _count(value: Any) -> int:
    """A status row's count as an integer; anything that is not one reads 0, as the gateway's asInt reads it."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _tools_lines(top_tools: Sequence[Tuple[str, int, Mapping[str, int]]]) -> List[str]:
    """The tools block: the label and ``none`` for no pairs, else the label and one row per pair."""
    if not top_tools:
        return [f"{TOP_TOOLS_LABEL}: {NONE}"]
    lines = [f"{TOP_TOOLS_LABEL}:"]
    status_order = TOOL_STATUS_ORDER + (OTHER_BUCKET,)
    for pair, total, by_status in top_tools:
        breakdown = ", ".join(f"{s} {by_status[s]}" for s in status_order if by_status.get(s))
        lines.append(f"  {pair}: {total}" + (f" ({breakdown})" if breakdown else ""))
    return lines


@functools.lru_cache(maxsize=1)
def template_tokens() -> frozenset:
    """Every token the block prints with no source at all: the template's own vocabulary."""
    empty = Snapshot(generated_at=TEMPLATE_EPOCH, cr={}, server_version=None, litellm_config=None,
                     tasks=None, litellm_metrics=None, sources={})
    return frozenset(TOKEN_PATTERN.findall(render(empty))) | TEMPLATE_EXTRA_TOKENS


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


def _without_model_line(text: str) -> str:
    """The block less its first ``model:`` line, the one line printed by design.

    A deployment name can carry a spec identifier as a segment
    (``hosted_vllm/acme/llama-8b`` beside an ``org`` of ``acme``); the line is
    never the reason a block is refused, and every other line stays audited.
    """
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line.startswith(MODEL_LINE_PREFIX):
            del lines[index]
            break
    return "\n".join(lines)


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
    model = model_from_status(status)
    if model is None:
        model = model_from_litellm_config(snapshot.litellm_config) or UNAVAILABLE
    interfaces = usage.get("activeInterfaces") or []
    environment = {
        "version": (spec.get("deployment") or {}).get("tag") or UNKNOWN,
        "mode": spec.get("mode") or DEFAULT_MODE,
        "model": model,
        "kubernetes": _server_version(snapshot.server_version),
        "installed": f"{_install_age(cr, now)}; ready: {_ready(cr)}",
        "interfaces": ", ".join(sorted(str(i) for i in interfaces)) or NONE,
    }
    lines.extend(f"  {key}: {environment[key]}" for key in ENVIRONMENT_KEYS)
    if snapshot.scale:
        scale = snapshot.scale
        lines.append(
            f"  scale: namespaces {scale.get('namespaces', 0)}, "
            f"deployments {scale.get('deployments', 0)}, "
            f"pods {scale.get('pods', 0)}, "
            f"nodes {scale.get('nodes', 0)}, "
            f"crds {scale.get('crds', 0)} (kcc {scale.get('kcc', 0)})"
        )
    lines.append("usage:")
    for counter in USAGE_COUNTERS:
        value = usage.get(counter)
        suffix = USAGE_READINGS.get(counter)
        if suffix is not None:
            shown = USAGE_NO_READING if value is None else int(value)
            lines.append(f"  {counter}: {shown}{suffix}")
        else:
            lines.append(f"  {counter}: {int(value or 0)}")
    lines.append(f"  lastActive: {usage.get('lastActiveTime') or NEVER}")
    lines.append(f"  lastHumanActive: {usage.get('lastHumanActiveTime') or NEVER}")
    # The broker's own text first; the operator's reading of it in the status
    # when the broker could not be read; unavailable when neither is there.
    status_tools = usage.get(TOP_TOOLS_STATUS_KEY)
    if snapshot.broker_metrics is not None:
        lines.extend(_tools_lines(summarise_broker_tools(parse_prometheus(snapshot.broker_metrics))))
    elif isinstance(status_tools, list):
        lines.extend(_tools_lines(summarise_status_tools(status_tools)))
    else:
        lines.append(f"{TOP_TOOLS_LABEL}: {UNAVAILABLE}")
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


def _running_pods(options: Options) -> Tuple[Optional[str], Optional[str]]:
    """The running gateway and credential-proxy pod names from one pod list."""
    raw = _optional(options, "get", "pods", "-n", options.namespace, "-o", "json")
    if raw is None:
        return None, None
    try:
        items = json.loads(raw).get("items") or []
    except ValueError:
        return None, None
    gateway: Optional[str] = None
    broker: Optional[str] = None
    for item in items:
        metadata = item.get("metadata") or {}
        name = metadata.get("name") or ""
        running = (item.get("status") or {}).get("phase") == POD_PHASE_RUNNING
        if not running or metadata.get("deletionTimestamp"):
            continue
        if gateway is None and name.startswith(options.name + GATEWAY_POD_INFIX):
            gateway = name
        elif broker is None and name.startswith(options.name + BROKER_POD_INFIX):
            broker = name
    return gateway, broker


def _broker_metrics(options: Options, broker_pod: Optional[str]) -> Optional[str]:
    if not broker_pod:
        return None
    raw = _optional(
        options, "exec", "-n", options.namespace, broker_pod, "-c", BROKER_CONTAINER, "--",
        "sh", "-c", f"curl {CURL_FLAGS} -m {CURL_TIMEOUT_SECONDS} {BROKER_METRICS_URL}",
        timeout=KUBECTL_TIMEOUT_SECONDS + CURL_TIMEOUT_SECONDS,
    )
    return raw if raw and raw.strip() else None


def _cluster_scale(options: Options) -> Optional[Dict[str, int]]:
    """Cluster-wide object counts from ``-o name`` lines (integer totals only, never names)."""
    counts: Dict[str, int] = {}
    for label, resource in SCALE_RESOURCES:
        args = ["get", resource, "-o", "name"]
        if label in ("deployments", "pods"):
            args.insert(2, "--all-namespaces")
        raw = _optional(options, *args)
        if raw is None:
            return None
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        counts[label] = len(lines)
        if label == "crds":
            counts["kcc"] = sum(1 for line in lines if line.endswith(KCC_CRD_SUFFIX))
    return counts


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
    # The ConfigMap is the fallback for a status with no ``inference`` key (an
    # operator older than the field, no Ready write yet, or a served CRD that
    # prunes it); with the key present the listing would be fetched and thrown
    # away, so it is not made.
    status = cr.get("status") if isinstance(cr.get("status"), Mapping) else {}
    litellm_config = None if INFERENCE_STATUS_KEY in status else _litellm_config(options)
    tasks: Optional[Sequence[Mapping[str, Any]]] = None
    truncated = False
    metrics: Optional[str] = None
    broker_metrics: Optional[str] = None
    scale: Optional[Mapping[str, int]] = None
    reasons: Dict[str, str] = {}
    if not options.in_pod:
        reasons = {
            "session-kv": REASON_SKIPPED,
            "litellm-metrics": REASON_SKIPPED,
            "broker-metrics": REASON_SKIPPED,
            "scale": REASON_SKIPPED,
        }
    else:
        pod, broker_pod = _running_pods(options)
        broker_metrics = _broker_metrics(options, broker_pod)
        if broker_metrics is None:
            reasons["broker-metrics"] = REASON_NO_BROKER_POD if not broker_pod else REASON_EXEC
        scale = _cluster_scale(options)
        if scale is None:
            reasons["scale"] = REASON_EXEC
        if not pod:
            reasons["session-kv"] = REASON_NO_POD
            reasons["litellm-metrics"] = REASON_NO_POD
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
        sources={
            "session-kv": tasks is not None,
            "litellm-metrics": metrics is not None,
            "broker-metrics": broker_metrics is not None,
            "scale": scale is not None,
        },
        source_reasons=reasons, broker_metrics=broker_metrics, scale=scale,
    )


def main(argv: Optional[Sequence[str]] = None, stdout: TextIO = sys.stdout, stderr: TextIO = sys.stderr) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--namespace", "-n", default=DEFAULT_NAMESPACE)
    parser.add_argument("--name", default=DEFAULT_AGENT_NAME, help="the PlatformAgent resource")
    parser.add_argument("--context")
    parser.add_argument("--kubeconfig")
    parser.add_argument("--no-exec", action="store_true", help="read the resource only; skip the pod and cluster-scale sources")
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
    leaked = audit(_without_model_line(text), snapshot.identifiers)
    if leaked:
        print(f"feedback-summary: refused to print: the block names {', '.join(leaked)}", file=stderr)
        return EXIT_LEAK
    print(text, file=stdout)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
