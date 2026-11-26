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

An object that asks for zero replicas is skipped by every heuristic. A
Gateway's own ``Programmed=False`` row is dropped when its in-cluster proxy
Deployment, found from the GatewayClass's ``controllerName`` in the Gateway's
namespace (and Envoy Gateway's controller namespace), asks for zero replicas:
the gateway is switched off on purpose. Its other rows stay.

The script never mutates. It prints a table (or ``--json``) and always ends
with ``stalled resources: <count>``, ``0`` on a healthy namespace.

Cluster mode (``--namespaces``) is the stall watch's: it reads each kind once
across every namespace, pages the kinds the credential broker lets a raw read
page (``RAW_COLLECTIONS``: ``kubectl get --raw <collection>?limit=``, the
reply bounded a page at a time) and the rest with one chunked ``kubectl get
<kind> --all-namespaces``, splits what came back per namespace in Python, and
prints one JSON report per named namespace, each the report the namespace
read would have printed. A kind no cluster-wide read could read whole (the
broker cut the reply at its output cap, the read failed, or a ``kind/name``
that names one object) is listed under ``fallback`` and not analysed; the
watch reads it per namespace as before. The Warning events are read the same
way, and ``events_fallback`` says when they were not. A fleet of hundreds of
namespaces costs kinds-plus-one reads per cluster rather than per namespace.

Usage::

    stall_report.py --namespace payments
    stall_report.py --namespace payments --kind gateways,httproutes
    stall_report.py --namespace payments --threshold-minutes 60 --json
    stall_report.py --namespace payments --workers 6
    stall_report.py --namespaces payments,billing --kind deployments --extra-kinds billing=sqlinstances.sql.cnrm.cloud.google.com --json
"""

from __future__ import annotations

import argparse
import ast
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import functools
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import types
import urllib.parse
import weakref

_DEFAULT_KUBECTL = "kubectl"
KUBECTL = _DEFAULT_KUBECTL
_ORIG_SUBPROCESS_RUN = subprocess.run
_SHIM_LOCK = threading.Lock()
_SHIM_CACHE: dict[
    tuple, tuple[object, tuple[tuple[str, str, int, int, int, int, int, int], ...], object]
] = {}
_SHIM_LOADED_SIGS: weakref.WeakKeyDictionary[object, tuple[int, int, int, int, int, int]] = (
    weakref.WeakKeyDictionary()
)
# Bounded entry cap for the shim validation cache keyed by PATH/stat/module state.
_SHIM_CACHE_MAX = 64
# Upper bound on shim/wrapper source size read during in-process validation (512 KiB).
_MAX_SHIM_BYTES = 512 * 1024
_SHIM_MODULE_NAME = "credential_proxy_client"
_SHIM_EXECUTE_CAPTURED_ATTR = "execute_captured"
_SHIM_EXEC_ENDPOINTS_ATTR = "exec_endpoints"
_SHIM_FILE_ATTR = "__file__"
_SHIM_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
_SHIM_READ_MODE = "rb"
_SHIM_TEXT_ENCODING = "utf-8"
_SHIM_COMPILE_MODE = "exec"
_MISSING_STAT_SIG = (-1, -1, -1, -1, -1, -1)
_DOCSTRING_DELIM = '"""'
_COMMENT_PREFIX = "#"
_SHIM_HEADER = (
    b'#!/usr/bin/env python3\n'
    b'"""Submit a supported CLI argv vector to the paired credential proxy."""\n'
)
_LAUNCHER_HEADER = (
    b'#!/usr/bin/env python3\n'
    b'"""Start the credential proxy client from its module.\n'
)
_LAUNCHER_BODY = (
    "import os",
    "import sys",
    "sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))",
    "from credential_proxy_client import main",
    "main()",
)
_LAUNCHER_CLIENT_NAME = "credential_proxy_client.py"
_SESSION_KUBECTL_SHEBANG = "#!/bin/sh"
_SESSION_KUBECTL_SHEBANG_BYTES = b"#!/bin/sh\n"
_SESSION_KUBECTL_CLIENT_VAR = "CLIENT"
_SESSION_KUBECTL_KUBE_PROXY_IF = 'if [ -n "${KUBE_PROXY_URL:-}" ]; then'
_SESSION_KUBECTL_BROKER_TAIL = (
    'if [ -n "${CREDENTIAL_PROXY_URL:-}" ] || [ -n "${CREDENTIAL_PROXY_EXEC_URL:-}" ]; then',
    'exec "$CLIENT" kubectl "$@"',
    "fi",
    'exec "$REAL_KUBECTL" "$@"',
)
_SHELL_FUNC_CLOSE = "}"
_SH_NEWLINE = "\n"
_SH_CARRIAGE_RETURN = "\r"
_SH_NUL = "\x00"
_SH_BLANKS = " \t"
_SH_SINGLE_QUOTE = "'"
_SH_DOUBLE_QUOTE = '"'
_SH_BACKSLASH = "\\"
_SH_BACKTICK = "`"
_SH_DOLLAR = "$"
_SH_HEREDOC_OP = "<<"
_SH_PARAM_PREFIX = "${"
_SH_ARITH_PREFIX = "$(("
_SH_ARITH_SUFFIX = "))"
_SH_CMDSUB_PREFIX = "$("
_SH_OPEN_PAREN = "("
_SH_CLOSE_PAREN = ")"
_SH_OPEN_BRACE = "{"
_SH_CLOSE_BRACE = "}"
_SH_SEMICOLON = ";"
_SH_PIPE = "|"
_SH_AMP = "&"
_SH_CASE_END = ";;"
_SH_ESCAPE_LEN = 2
_SH_QUOTED_PLACEHOLDER = "_"
_SH_WORD_DELIMITERS = frozenset((" ", "\t", ";", "&", "|", "(", ")", "<", ">"))
_SH_PARAM_FORBIDDEN = frozenset(('"', "'", "\\", "$", "`", "{", "(", ")"))
_SH_ARITH_FORBIDDEN = frozenset(('"', "'", "\\", "`", "{", "}", ";", "("))
_SH_TRAILING_CONTINUATION_OPS = (_SH_PIPE, _SH_AMP)
_SH_BLOCK_IF = "if"
_SH_BLOCK_CASE = "case"
_SH_ELSE_WORD = "else"
_SH_FI_WORD = "fi"
_SH_ESAC_WORD = "esac"
_SH_CONTROL_WORDS = frozenset(
    ("if", "then", "else", "elif", "fi", "case", "esac", "for", "while", "until", "do", "done")
)
_PARENT_DIR_SEGMENT = ".."
_POSIX_PATH_SEP = "/"
_SAFE_READONLY_RE = re.compile(r"^readonly ([A-Za-z_][A-Za-z0-9_]*)=([A-Za-z0-9._/@:+-]+)\Z")
_SHELL_FUNC_START_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\(\)[ \t]*\{\Z")
_SAFE_ABS_PATH_RE = re.compile(r"^/[A-Za-z0-9._/@+-]+\Z")
_SH_WORD_SPLIT_RE = re.compile(r"[ \t;&|<>]+")
_SH_IF_OR_ELIF_THEN_RE = re.compile(r"^(if|elif)[ \t]+([^;)]+);[ \t]*then\Z")
_SH_CASE_IN_RE = re.compile(r"^case[ \t]+([^ \t;&|()]+)[ \t]+in\Z")
_CREDENTIAL_PROXY_URL_ENV = "CREDENTIAL_PROXY_URL"
_CREDENTIAL_PROXY_EXEC_URL_ENV = "CREDENTIAL_PROXY_EXEC_URL"
_KUBE_PROXY_URL_ENV = "KUBE_PROXY_URL"
_NAME_RESOLVER_EXISTS_ATTR = "exists"
_K8S_ITEMS_KEY = "items"
_K8S_KIND_KEY = "kind"
_K8S_SPEC_KEY = "spec"
_K8S_METADATA_KEY = "metadata"
_K8S_NAMESPACE_KEY = "namespace"
_ARGS_SEPARATOR = " "
_NON_OBJECT_JSON_ERROR = "kubectl {args} returned non-object JSON ({rc}): {kind}"
# Caps memoized RFC 3339 timestamps per process across repeated managedFields,
# condition transitions, and event spans without unbounded growth.
_PARSE_TIME_CACHE_MAX = 4096
# Default concurrent kubectl reads for standalone scans: matches
# credential_proxy.DEFAULT_SESSION_MAX_CONCURRENT_COMMANDS (2, immediate
# non-retried 503 past the cap) and half of
# credential_proxy.DEFAULT_PROFILE_MAX_CONCURRENT_COMMANDS (4, matching
# workload_triage.FALLBACK_CONCURRENCY) so one scan leaves profile and pool
# slots free for sibling workers. Callers with higher budgets may pass
# --workers up to MAX_COLLECT_WORKERS; callers that already parallelize
# across clusters (such as stall_watch.py under BROKER_SLOTS) pass
# --workers 1 so nested pools do not multiply concurrent calls.
DEFAULT_COLLECT_WORKERS = 2
MIN_COLLECT_WORKERS = 1
MAX_COLLECT_WORKERS = 6
COLLECT_WORKERS = DEFAULT_COLLECT_WORKERS
WORKERS_FLAG = "--workers"
_WORKERS_HELP = (
    f"maximum concurrent kubectl reads per scan (clamped to "
    f"{MIN_COLLECT_WORKERS}..{MAX_COLLECT_WORKERS}, default {DEFAULT_COLLECT_WORKERS})"
)
_WORKERS_MIN_ERROR = f"{WORKERS_FLAG} must be at least {MIN_COLLECT_WORKERS}"
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

# Cluster mode's vocabulary. A cluster-wide read is one of two shapes. The
# kinds in RAW_COLLECTIONS are read through `kubectl get --raw
# <collection>?limit=N`, following `metadata.continue`, which is how the
# fleet collectors page a large kind: each reply is one page, so the broker's
# output cap (CREDENTIAL_PROXY_MAX_OUTPUT_BYTES, 8 MiB as the operator deploys
# it) bounds a page and never the kind. The table is the namespaced part of
# the broker's own raw-path allowlist (command_policy.KUBECTL_RAW_COLLECTIONS,
# less the resources a scan skips); a raw read of any other path is refused
# there, so every other kind, a CRD or a Config Connector kind among them, is
# read with one `kubectl get <kind> --all-namespaces --chunk-size=500`, where
# kubectl pages its requests and prints one reply. That reply the broker can
# cut, and a cut reply, or a failed read, sends the kind back to the
# per-namespace path, so nothing is lost and a busy kind costs what it did.
# The raw API returns `managedFields` without being asked and no `kind` on
# an item, so items are stamped from the table.
ALL_NAMESPACES_FLAG = "--all-namespaces"
# The page size the fleet collectors read at (collector_run.PAGE_SIZE), and
# kubectl's request page for the chunked read.
PAGE_SIZE = 500
CHUNK_SIZE_ARG = f"--chunk-size={PAGE_SIZE}"
RAW_FLAG = "--raw"
RAW_LIMIT_KEY = "limit"
RAW_CONTINUE_KEY = "continue"
RAW_FIELD_SELECTOR_KEY = "fieldSelector"
RAW_QUERY_MARK = "?"
# A page the broker cut is asked for again at half the size, down to this
# floor; an object list too fat for a page of this many is left to the
# per-namespace read.
RAW_PAGE_SIZE_MIN = 25
RAW_PAGE_HALVING = 2
# A server that keeps handing out continue tokens is stopped here; the fleet
# collectors stop at the same count.
RAW_MAX_PAGES = 10000
# A raw collection: its path, the item kind, the apiVersion the items carry.
# Keyed by the bare plural and by the api-resources name with its group, the
# two spellings a kind list carries.
RAW_COLLECTIONS: dict[str, tuple[str, str, str]] = {
    name: entry
    for names, entry in (
        (("pods",), ("/api/v1/pods", "Pod", "v1")),
        (("services",), ("/api/v1/services", "Service", "v1")),
        (("persistentvolumeclaims",), ("/api/v1/persistentvolumeclaims", "PersistentVolumeClaim", "v1")),
        (("serviceaccounts",), ("/api/v1/serviceaccounts", "ServiceAccount", "v1")),
        (("limitranges",), ("/api/v1/limitranges", "LimitRange", "v1")),
        (("resourcequotas",), ("/api/v1/resourcequotas", "ResourceQuota", "v1")),
        (("deployments", "deployments.apps"), ("/apis/apps/v1/deployments", "Deployment", "apps/v1")),
        (("daemonsets", "daemonsets.apps"), ("/apis/apps/v1/daemonsets", "DaemonSet", "apps/v1")),
        (("replicasets", "replicasets.apps"), ("/apis/apps/v1/replicasets", "ReplicaSet", "apps/v1")),
        (("statefulsets", "statefulsets.apps"), ("/apis/apps/v1/statefulsets", "StatefulSet", "apps/v1")),
        (
            ("horizontalpodautoscalers", "horizontalpodautoscalers.autoscaling"),
            ("/apis/autoscaling/v2/horizontalpodautoscalers", "HorizontalPodAutoscaler", "autoscaling/v2"),
        ),
        (("cronjobs", "cronjobs.batch"), ("/apis/batch/v1/cronjobs", "CronJob", "batch/v1")),
        (("jobs", "jobs.batch"), ("/apis/batch/v1/jobs", "Job", "batch/v1")),
        (("ingresses", "ingresses.networking.k8s.io"), ("/apis/networking.k8s.io/v1/ingresses", "Ingress", "networking.k8s.io/v1")),
        (
            ("networkpolicies", "networkpolicies.networking.k8s.io"),
            ("/apis/networking.k8s.io/v1/networkpolicies", "NetworkPolicy", "networking.k8s.io/v1"),
        ),
        (("poddisruptionbudgets", "poddisruptionbudgets.policy"), ("/apis/policy/v1/poddisruptionbudgets", "PodDisruptionBudget", "policy/v1")),
    )
    for name in names
}
# The Warning events, cluster-wide: the raw collection, with the selector the
# per-namespace read passes to kubectl as a query parameter.
EVENTS_COLLECTION = ("/api/v1/events", "Event", "v1")
# What the credential proxy shim prints on stderr, and exits with, when the
# broker cut a reply at its output cap. An older shim exited as kubectl did,
# so the text is read before the exit code (stall_watch.py names the same
# text; this script runs alone in the sandbox and imports nothing).
PROXY_TRUNCATED_TEXT = "credential proxy output truncated"
EXIT_OUTPUT_TRUNCATED = 74
# The referent listing cluster mode resolves names from: namespace and name
# only, as `-o name` is per namespace, so Secret bodies never cross.
NO_HEADERS_FLAG = "--no-headers"
NAME_COLUMNS = "custom-columns=NAMESPACE:.metadata.namespace,NAME:.metadata.name"
NAME_COLUMN_COUNT = 2
# The cluster report's keys, read by stall_watch.py, and the per-namespace
# report's, which the cluster report nests one of per namespace.
REPORT_NAMESPACES_KEY = "namespaces"
REPORT_FALLBACK_KEY = "fallback"
REPORT_EVENTS_FALLBACK_KEY = "events_fallback"
REPORT_NAMESPACE_KEY = "namespace"
REPORT_COUNT_KEY = "stalled_resources"
REPORT_FINDINGS_KEY = "findings"
# `--extra-kinds <namespace>=<kinds>`: the kinds one namespace is asked about
# beyond the list every namespace gets.
EXTRA_KINDS_SEPARATOR = "="
NAMESPACES_SEPARATOR = ","
KINDS_SEPARATOR = ","

# How long an object may sit without progress before it is a stall. One default
# for every kind, with per-kind overrides where the API itself names a horizon:
# a Deployment's progressDeadlineSeconds defaults to 600, so a rollout still
# not complete after ten minutes has already exceeded what its own controller
# tolerates. --threshold-minutes overrides both for one run.
DEFAULT_STALL_MINUTES = 15
STALL_MINUTES_BY_KIND: dict[str, int] = {
    "Deployment": 10,
}
# Config Connector holds Ready=False with reason Updating for the whole of a
# create or update and Deleting for a delete, and a SQLInstance or a
# ContainerCluster takes longer than DEFAULT_STALL_MINUTES to create, so an
# object in either state is in progress, not stalled, at any age: neither its
# Ready condition nor its generation lag is a row (the daily kcc-object-wedged
# audit check skips the same two reasons). DependencyNotReady lasts as long as
# the referenced resource's own create, so it is held to
# DEPENDENCY_STALL_MINUTES, past any ordinary create, before it is a row; a
# dependency still not ready after an hour is the wedge the audit check finds.
READY_CONDITION_TYPE = "Ready"
IN_PROGRESS_READY_REASONS = frozenset({"Updating", "Deleting"})
DEPENDENCY_NOT_READY_REASON = "DependencyNotReady"
DEPENDENCY_STALL_MINUTES = 60

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

# A Gateway whose in-cluster proxy is scaled to zero on purpose reports
# Programmed=False (Envoy Gateway: reason NoResources). That one row is dropped
# when the proxy Deployment exists and asks for zero replicas; every other row
# of the Gateway stays, since a scale to zero cannot cause it, and routes are
# left alone (their backends at zero raise only BackendsAvailable=False, which
# no heuristic reads). The proxy is read only when that row has been reported,
# so a healthy namespace costs no extra read.
GATEWAY_KIND = "Gateway"
SCALED_TO_ZERO_DETAIL = "Programmed=False"
DEPLOYMENTS_RESOURCE = "deployments"
GATEWAY_CLASSES_RESOURCE = "gatewayclasses.gateway.networking.k8s.io"
LABEL_SELECTOR_FLAG = "-l"
# How an in-cluster gateway controller labels the proxy Deployment it runs for
# a Gateway, by the GatewayClass's spec.controllerName, and the namespace it
# runs it in besides the Gateway's own: Envoy Gateway runs it in its controller
# namespace by default. Every other controller is looked up by the label the
# Gateway API recommends for generated resources, in the Gateway's namespace
# only. The lookup stays out of other namespaces, so nobody can silence a
# Gateway by planting a zero-replica Deployment labelled for it elsewhere. A
# controller with no in-cluster proxy (GKE's managed classes) matches no
# Deployment, so its Gateways keep the row.
ENVOY_GATEWAY_NAMESPACE = "envoy-gateway-system"
PROXY_SELECTORS: dict[str, tuple[str, tuple[str, ...]]] = {
    "gateway.envoyproxy.io/gatewayclass-controller": (
        "gateway.envoyproxy.io/owning-gateway-name={name},"
        "gateway.envoyproxy.io/owning-gateway-namespace={namespace}",
        (ENVOY_GATEWAY_NAMESPACE,),
    ),
}
DEFAULT_PROXY_SELECTOR: tuple[str, tuple[str, ...]] = ("gateway.networking.k8s.io/gateway-name={name}", ())

# The stall watch's opt-out annotation. An object that carries it is still
# reported, and the table is unchanged; each of its --json rows says so in
# EXEMPT_FIELD, and stall_watch.py leaves those rows out of its alerts.
# stall_watch.py names the same key and value: this script runs alone in the
# sandbox, so it imports nothing.
EXEMPT_ANNOTATION = "kubeagents.x-k8s.io/stall-watch"
EXEMPT_VALUE = "exempt"
EXEMPT_FIELD = "exempt"

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

# What kubectl prints when a listing is refused for good rather than failing
# for the moment: the identity may not list the kind, or the cluster does not
# serve it. A listing that fails any other way (a timeout, a 5xx, a reply the
# broker cut) may answer the next time it is asked, so a resolver treats the
# two apart: a refused kind is not asked for again, a kind unreadable this
# time is asked for in the next namespace.
LISTING_REFUSAL_MARKERS = ("(Forbidden)", "doesn't have a resource type", "could not find the requested resource")


class ListingRefused:
    """The lister's answer for a kind refused outright; `None` is a kind not
    readable this time. `NameResolver` reads both as unresolvable."""


REFUSED = ListingRefused()


# --------------------------------------------------------------------------
# kubectl
# --------------------------------------------------------------------------


def _has_broker_url() -> bool:
    return bool(
        os.environ.get(_CREDENTIAL_PROXY_URL_ENV, "").strip()
        or os.environ.get(_CREDENTIAL_PROXY_EXEC_URL_ENV, "").strip()
    )


def _is_owned_regular_file(info: os.stat_result) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and 0 < info.st_size <= _MAX_SHIM_BYTES
        and info.st_uid in (0, os.geteuid())
        and not (info.st_mode & stat.S_IWOTH)
    )


def _stat_sig(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_uid, info.st_mode)


def _watch_entry(
    path: str, info: os.stat_result | None
) -> tuple[str, str, int, int, int, int, int, int]:
    try:
        real = os.path.realpath(path)
    except OSError:
        real = ""
    if info is None:
        return (path, real, *_MISSING_STAT_SIG)
    return (path, real, *_stat_sig(info))


def _watched_unchanged(
    watched: tuple[tuple[str, str, int, int, int, int, int, int], ...],
) -> bool:
    for watch_path, real, dev, ino, size, mtime_ns, uid, mode in watched:
        try:
            cur_real = os.path.realpath(watch_path)
            sig = _stat_sig(os.stat(watch_path))
        except OSError:
            cur_real = ""
            sig = _MISSING_STAT_SIG
        if dev == -1:
            if sig != _MISSING_STAT_SIG:
                return False
            continue
        if cur_real != real or sig != (dev, ino, size, mtime_ns, uid, mode):
            return False
    return True


def _read_bounded(
    path: str, info: os.stat_result | None = None
) -> tuple[os.stat_result, bytes] | None:
    try:
        if info is None:
            info = os.stat(path)
        if not _is_owned_regular_file(info):
            return None
        fd = os.open(path, _SHIM_OPEN_FLAGS)
    except OSError:
        return None
    try:
        handle = os.fdopen(fd, _SHIM_READ_MODE)
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return None
    try:
        with handle:
            fd_info = os.fstat(handle.fileno())
            if not _is_owned_regular_file(fd_info):
                return None
            raw = handle.read(_MAX_SHIM_BYTES + 1)
    except OSError:
        return None
    if not raw or len(raw) > _MAX_SHIM_BYTES:
        return None
    return fd_info, raw


def _is_launcher_script(raw: bytes) -> bool:
    """Whether `raw` is the precompiled shim launcher (`credential-proxy-exec.py`)."""
    if not raw.startswith(_LAUNCHER_HEADER):
        return False
    try:
        text = raw.decode(_SHIM_TEXT_ENCODING)
    except UnicodeDecodeError:
        return False
    doc_start = text.find(_DOCSTRING_DELIM)
    if doc_start < 0:
        return False
    doc_end = text.find(_DOCSTRING_DELIM, doc_start + len(_DOCSTRING_DELIM))
    if doc_end < 0:
        return False
    body_lines = []
    for line in text[doc_end + len(_DOCSTRING_DELIM) :].splitlines():
        code = line.split(_COMMENT_PREFIX, 1)[0].strip()
        if code:
            body_lines.append(code)
    return tuple(body_lines) == _LAUNCHER_BODY


def _scan_sh_param_expansion(line: str, start: int) -> int | None:
    """Scan a single-line `${...}` parameter expansion starting after `${`."""
    pos = start
    length = len(line)
    while pos < length:
        ch = line[pos]
        if ch == _SH_CLOSE_BRACE:
            return pos + 1 if pos > start else None
        if ch in _SH_PARAM_FORBIDDEN:
            return None
        pos += 1
    return None


def _scan_sh_arith_expansion(line: str, start: int) -> int | None:
    """Scan a single-line `$((...))` arithmetic expansion starting after `$((`."""
    pos = start
    length = len(line)
    while pos < length:
        ch = line[pos]
        if ch == _SH_CLOSE_PAREN:
            if pos > start and line.startswith(_SH_ARITH_SUFFIX, pos):
                return pos + len(_SH_ARITH_SUFFIX)
            return None
        if ch in _SH_ARITH_FORBIDDEN:
            return None
        pos += 1
    return None


def _scan_sh_double_quote(line: str, start: int) -> int | None:
    """Scan a single-line `"..."` string starting after the opening `"`."""
    pos = start
    length = len(line)
    while pos < length:
        ch = line[pos]
        if ch == _SH_DOUBLE_QUOTE:
            return pos + 1
        if ch == _SH_BACKSLASH:
            if pos + 1 >= length:
                return None
            pos += _SH_ESCAPE_LEN
            continue
        if ch == _SH_BACKTICK:
            return None
        if ch == _SH_DOLLAR:
            if line.startswith(_SH_CMDSUB_PREFIX, pos):
                return None
            if line.startswith(_SH_PARAM_PREFIX, pos):
                next_pos = _scan_sh_param_expansion(line, pos + len(_SH_PARAM_PREFIX))
                if next_pos is None:
                    return None
                pos = next_pos
                continue
        pos += 1
    return None


def _scan_sh_logical_line(
    lines: list[str], start_index: int, *, allow_arith: bool
) -> tuple[str, int] | None:
    """Scan one logical POSIX `/bin/sh` line, rejecting multi-line quotes/heredocs/subshells.

    Replaces balanced quoted strings, escapes, and single-line `${...}` / `$((...))`
    expansions with `_SH_QUOTED_PLACEHOLDER` and strips `#` comments at word start so
    subsequent control-flow checks see only unquoted shell syntax. Joins unquoted
    trailing-backslash continuations only at blank token boundaries.
    """
    code_chars: list[str] = []
    has_non_blank = False
    at_word_start = True
    idx = start_index
    continued = True
    while continued:
        if idx >= len(lines):
            return None
        line = lines[idx]
        idx += 1
        continued = False
        pos = 0
        length = len(line)
        while pos < length:
            ch = line[pos]
            if ch == _COMMENT_PREFIX and at_word_start:
                break
            if ch == _SH_SINGLE_QUOTE:
                end = line.find(_SH_SINGLE_QUOTE, pos + 1)
                if end < 0:
                    return None
                code_chars.append(_SH_QUOTED_PLACEHOLDER)
                has_non_blank = True
                at_word_start = False
                pos = end + 1
                continue
            if ch == _SH_DOUBLE_QUOTE:
                next_pos = _scan_sh_double_quote(line, pos + 1)
                if next_pos is None:
                    return None
                code_chars.append(_SH_QUOTED_PLACEHOLDER)
                has_non_blank = True
                at_word_start = False
                pos = next_pos
                continue
            if ch == _SH_BACKSLASH:
                if pos + 1 >= length:
                    if not has_non_blank or pos == 0 or line[pos - 1] not in _SH_BLANKS:
                        return None
                    continued = True
                    break
                code_chars.append(_SH_QUOTED_PLACEHOLDER)
                has_non_blank = True
                at_word_start = False
                pos += _SH_ESCAPE_LEN
                continue
            if ch == _SH_BACKTICK or line.startswith(_SH_HEREDOC_OP, pos):
                return None
            if ch in (_SH_OPEN_PAREN, _SH_OPEN_BRACE, _SH_CLOSE_BRACE):
                return None
            if ch == _SH_DOLLAR:
                if line.startswith(_SH_PARAM_PREFIX, pos):
                    next_pos = _scan_sh_param_expansion(line, pos + len(_SH_PARAM_PREFIX))
                    if next_pos is None:
                        return None
                    code_chars.append(_SH_QUOTED_PLACEHOLDER)
                    has_non_blank = True
                    at_word_start = False
                    pos = next_pos
                    continue
                if line.startswith(_SH_ARITH_PREFIX, pos):
                    if not allow_arith:
                        return None
                    next_pos = _scan_sh_arith_expansion(line, pos + len(_SH_ARITH_PREFIX))
                    if next_pos is None:
                        return None
                    code_chars.append(_SH_QUOTED_PLACEHOLDER)
                    has_non_blank = True
                    at_word_start = False
                    pos = next_pos
                    continue
                if line.startswith(_SH_CMDSUB_PREFIX, pos):
                    return None
            code_chars.append(ch)
            if ch not in _SH_BLANKS:
                has_non_blank = True
            at_word_start = ch in _SH_WORD_DELIMITERS
            pos += 1
    return "".join(code_chars), idx


def _sh_has_control_word(code: str) -> bool:
    """Whether `code` contains any POSIX shell control keyword as a standalone word."""
    return any(token in _SH_CONTROL_WORDS for token in _SH_WORD_SPLIT_RE.split(code))


def _session_kubectl_client_path(raw: bytes) -> str | None:
    """Extract `CLIENT` from the worker image's `session-kubectl.sh` wrapper.

    Fail-closed: accepts only the known wrapper grammar (shebang, comments,
    `readonly` assignments, shell helper functions, the `KUBE_PROXY_URL` block,
    and the exact broker fallback tail) so any unfamiliar wrapper falls back to
    `subprocess.run`.
    """
    try:
        text = raw.decode(_SHIM_TEXT_ENCODING)
    except UnicodeDecodeError:
        return None
    if _SH_CARRIAGE_RETURN in text or _SH_NUL in text:
        return None
    lines = text.split(_SH_NEWLINE)
    if not lines or lines[0] != _SESSION_KUBECTL_SHEBANG:
        return None
    readonly_vars: dict[str, str] = {}
    index = 1
    in_func = False
    found_kube_proxy_if = False
    while index < len(lines):
        line = lines[index]
        if in_func:
            if line == _SHELL_FUNC_CLOSE:
                in_func = False
                index += 1
                continue
            scanned = _scan_sh_logical_line(lines, index, allow_arith=True)
            if scanned is None:
                return None
            code, index = scanned
            if code.strip(_SH_BLANKS).endswith(_SH_TRAILING_CONTINUATION_OPS):
                return None
            continue
        stripped = line.strip(_SH_BLANKS)
        index += 1
        if not stripped or stripped.startswith(_COMMENT_PREFIX):
            continue
        if stripped == _SESSION_KUBECTL_KUBE_PROXY_IF:
            found_kube_proxy_if = True
            break
        match = _SAFE_READONLY_RE.match(stripped)
        if match:
            name, value = match.groups()
            if name in readonly_vars:
                return None
            readonly_vars[name] = value
            continue
        if _SHELL_FUNC_START_RE.match(line):
            in_func = True
            continue
        return None
    if in_func or not found_kube_proxy_if:
        return None
    block_stack = [_SH_BLOCK_IF]
    while index < len(lines) and block_stack:
        scanned = _scan_sh_logical_line(lines, index, allow_arith=False)
        if scanned is None:
            return None
        code, index = scanned
        code = code.strip(_SH_BLANKS)
        if not code:
            continue
        if code.endswith(_SH_TRAILING_CONTINUATION_OPS):
            return None
        if code == _SH_FI_WORD:
            if block_stack[-1] != _SH_BLOCK_IF:
                return None
            block_stack.pop()
            continue
        if code == _SH_ELSE_WORD:
            if len(block_stack) <= 1 or block_stack[-1] != _SH_BLOCK_IF:
                return None
            continue
        if code == _SH_ESAC_WORD:
            if block_stack[-1] != _SH_BLOCK_CASE:
                return None
            block_stack.pop()
            continue
        if code == _SH_CASE_END:
            if block_stack[-1] != _SH_BLOCK_CASE:
                return None
            continue
        if_match = _SH_IF_OR_ELIF_THEN_RE.match(code)
        if if_match:
            keyword, cond = if_match.groups()
            if _sh_has_control_word(cond):
                return None
            if keyword == _SH_BLOCK_IF:
                block_stack.append(_SH_BLOCK_IF)
            elif len(block_stack) <= 1 or block_stack[-1] != _SH_BLOCK_IF:
                return None
            continue
        case_match = _SH_CASE_IN_RE.match(code)
        if case_match:
            if case_match.group(1) in _SH_CONTROL_WORDS:
                return None
            block_stack.append(_SH_BLOCK_CASE)
            continue
        if _SH_CLOSE_PAREN in code:
            if (
                block_stack[-1] != _SH_BLOCK_CASE
                or code.count(_SH_CLOSE_PAREN) != 1
                or not code.endswith(_SH_CLOSE_PAREN)
                or _SH_SEMICOLON in code
                or _sh_has_control_word(code[:-1])
            ):
                return None
            continue
        if _sh_has_control_word(code):
            return None
    if block_stack:
        return None
    tail = tuple(
        line.strip(_SH_BLANKS)
        for line in lines[index:]
        if line.strip(_SH_BLANKS) and not line.strip(_SH_BLANKS).startswith(_COMMENT_PREFIX)
    )
    if tail != _SESSION_KUBECTL_BROKER_TAIL:
        return None
    client = readonly_vars.get(_SESSION_KUBECTL_CLIENT_VAR)
    if not client or not _SAFE_ABS_PATH_RE.match(client) or _PARENT_DIR_SEGMENT in client.split(_POSIX_PATH_SEP):
        return None
    return client


def _resolve_shim_target(
    path: str, path_info: os.stat_result, kube_proxy_active: bool
) -> tuple[
    str | None,
    os.stat_result | None,
    bytes | None,
    tuple[tuple[str, str, int, int, int, int, int, int], ...],
    os.stat_result | None,
]:
    bounded = _read_bounded(path, path_info)
    if bounded is None:
        return None, None, None, (), None
    path_fd_info, raw = bounded
    if raw.startswith(_SHIM_HEADER):
        return path, path_fd_info, raw, (), path_fd_info
    if _is_launcher_script(raw):
        target_path = os.path.join(os.path.dirname(os.path.realpath(path)), _LAUNCHER_CLIENT_NAME)
        target_bounded = _read_bounded(target_path)
        if target_bounded is None:
            return None, None, None, (_watch_entry(target_path, None),), path_fd_info
        target_info, target_raw = target_bounded
        watch = (_watch_entry(target_path, target_info),)
        if target_raw.startswith(_SHIM_HEADER):
            return target_path, target_info, target_raw, watch, path_fd_info
        return None, None, None, watch, path_fd_info
    if not kube_proxy_active and raw.startswith(_SESSION_KUBECTL_SHEBANG_BYTES):
        client_path = _session_kubectl_client_path(raw)
        if client_path is None:
            return None, None, None, (), path_fd_info
        client_bounded = _read_bounded(client_path)
        if client_bounded is None:
            return None, None, None, (_watch_entry(client_path, None),), path_fd_info
        client_info, client_raw = client_bounded
        client_watch = _watch_entry(client_path, client_info)
        if client_raw.startswith(_SHIM_HEADER):
            return client_path, client_info, client_raw, (client_watch,), path_fd_info
        if _is_launcher_script(client_raw):
            target_path = os.path.join(
                os.path.dirname(os.path.realpath(client_path)), _LAUNCHER_CLIENT_NAME
            )
            target_bounded = _read_bounded(target_path)
            if target_bounded is None:
                return (
                    None,
                    None,
                    None,
                    (client_watch, _watch_entry(target_path, None)),
                    path_fd_info,
                )
            target_info, target_raw = target_bounded
            target_watch = _watch_entry(target_path, target_info)
            if target_raw.startswith(_SHIM_HEADER):
                return (
                    target_path,
                    target_info,
                    target_raw,
                    (client_watch, target_watch),
                    path_fd_info,
                )
            return None, None, None, (client_watch, target_watch), path_fd_info
        return None, None, None, (client_watch,), path_fd_info
    return None, None, None, (), path_fd_info


def _loaded_module_key(loaded: object) -> tuple | None:
    if loaded is None:
        return None
    mod_file = getattr(loaded, _SHIM_FILE_ATTR, None)
    if not mod_file:
        return (id(loaded), None, None)
    try:
        mstat = os.stat(mod_file)
        return (id(loaded), os.path.realpath(mod_file), *_stat_sig(mstat))
    except OSError:
        return (id(loaded), mod_file, None)


def _parse_shim_ast(target_raw: bytes, filename: str) -> ast.Module | None:
    try:
        tree = ast.parse(target_raw, filename=filename)
    except (SyntaxError, ValueError):
        return None
    top_level_funcs = {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    if _SHIM_EXECUTE_CAPTURED_ATTR not in top_level_funcs or _SHIM_EXEC_ENDPOINTS_ATTR not in top_level_funcs:
        return None
    return tree


def _shim_client():
    """The credential_proxy_client module when `kubectl` on PATH dispatches to it.

    Covers all three layouts shipped by kube-agents:
    1. `kubectl` on PATH is a copy/symlink of `credential_proxy_client.py`;
    2. `kubectl` resolves to the precompiled module launcher (`credential-proxy-exec`);
    3. `kubectl` is the worker image's `session-kubectl.sh` wrapper with `KUBE_PROXY_URL`
       unset, delegating directly to `CLIENT` (`credential-proxy-exec`).

    When verified, calling `execute_captured` in-process runs the shim's own code
    minus a Python interpreter start per call. Anything else -- stock kubectl,
    `KUBE_PROXY_URL` active under `session-kubectl.sh`, a modified wrapper, a
    different shim version, no broker URL -- returns None and falls back to
    `subprocess.run`.
    """
    if KUBECTL != _DEFAULT_KUBECTL or subprocess.run is not _ORIG_SUBPROCESS_RUN:
        return None
    if not _has_broker_url():
        return None
    path = shutil.which(KUBECTL)
    if not path:
        return None
    kube_proxy_active = bool(os.environ.get(_KUBE_PROXY_URL_ENV, ""))
    with _SHIM_LOCK:
        try:
            path_real = os.path.realpath(path)
            info = os.stat(path)
        except OSError:
            return None
        if not _is_owned_regular_file(info):
            return None
        loaded = sys.modules.get(_SHIM_MODULE_NAME)
        key = (path, path_real, *_stat_sig(info), kube_proxy_active, _loaded_module_key(loaded))
        cached = _SHIM_CACHE.get(key)
        if cached is not None:
            mod, watched, cached_loaded = cached
            if cached_loaded is loaded and _watched_unchanged(watched):
                return mod
        if len(_SHIM_CACHE) >= _SHIM_CACHE_MAX:
            _SHIM_CACHE.clear()
        mod, watched, path_fd_info = _load_shim_client_with_watch(path, info, kube_proxy_active)
        if path_fd_info is not None and _stat_sig(path_fd_info) == _stat_sig(info):
            try:
                post_real = os.path.realpath(path)
            except OSError:
                post_real = ""
            if post_real == path_real:
                post_loaded = sys.modules.get(_SHIM_MODULE_NAME)
                post_key = (
                    path,
                    path_real,
                    *_stat_sig(path_fd_info),
                    kube_proxy_active,
                    _loaded_module_key(post_loaded),
                )
                _SHIM_CACHE[post_key] = (mod, watched, post_loaded)
        return mod


def _load_shim_client(
    path: str, path_info: os.stat_result | None = None, *, kube_proxy_active: bool = False
):
    mod, _watched, _path_fd_info = _load_shim_client_with_watch(path, path_info, kube_proxy_active)
    return mod


def _load_shim_client_with_watch(
    path: str, path_info: os.stat_result | None, kube_proxy_active: bool
) -> tuple[
    object, tuple[tuple[str, str, int, int, int, int, int, int], ...], os.stat_result | None
]:
    try:
        if path_info is None:
            path_info = os.stat(path)
    except OSError:
        return None, (), None
    if not _is_owned_regular_file(path_info):
        return None, (), None
    target_path, target_info, target_raw, watched, path_fd_info = _resolve_shim_target(
        path, path_info, kube_proxy_active
    )
    if target_path is None or target_info is None or target_raw is None:
        return None, watched, path_fd_info
    try:
        import credential_proxy_client  # noqa: PLC0415 - only when the shim is in use
    except ImportError:
        credential_proxy_client = None
    if credential_proxy_client is not None:
        module_file = getattr(credential_proxy_client, _SHIM_FILE_ATTR, None)
        if (
            not module_file
            or not hasattr(credential_proxy_client, _SHIM_EXECUTE_CAPTURED_ATTR)
            or not hasattr(credential_proxy_client, _SHIM_EXEC_ENDPOINTS_ATTR)
        ):
            return None, watched, path_fd_info
        mod_bounded = _read_bounded(module_file)
        if mod_bounded is None:
            return None, watched, path_fd_info
        mod_info, mod_raw = mod_bounded
        try:
            recorded_sig = _SHIM_LOADED_SIGS.get(credential_proxy_client)
        except TypeError:
            return None, watched, path_fd_info
        if recorded_sig is not None and recorded_sig != _stat_sig(mod_info):
            return None, watched, path_fd_info
        if target_raw != mod_raw:
            return None, watched, path_fd_info
        try:
            _SHIM_LOADED_SIGS[credential_proxy_client] = _stat_sig(mod_info)
        except TypeError:
            return None, watched, path_fd_info
        return credential_proxy_client, watched, path_fd_info
    # Fallback when running under `python3 -I -` (stall_watch.py) or from the
    # persona skill directory where credential_proxy_client.py is not on sys.path.
    target_real = os.path.realpath(target_path)
    tree = _parse_shim_ast(target_raw, target_real)
    if tree is None:
        return None, watched, path_fd_info
    try:
        mod = types.ModuleType(_SHIM_MODULE_NAME)
        mod.__file__ = target_real
        mod.__package__ = ""
        sys.modules[_SHIM_MODULE_NAME] = mod
        try:
            # Always execute the already-read, AST-validated tree so no untrusted
            # __pycache__/*.pyc or post-read TOCTOU swap can run in-process.
            exec(compile(tree, target_real, _SHIM_COMPILE_MODE), mod.__dict__)
        except Exception:  # noqa: BLE001
            sys.modules.pop(_SHIM_MODULE_NAME, None)
            return None, watched, path_fd_info
    except Exception:  # noqa: BLE001
        return None, watched, path_fd_info
    if hasattr(mod, _SHIM_EXECUTE_CAPTURED_ATTR) and hasattr(mod, _SHIM_EXEC_ENDPOINTS_ATTR):
        _SHIM_LOADED_SIGS[mod] = _stat_sig(target_info)
        return mod, watched, path_fd_info
    sys.modules.pop(_SHIM_MODULE_NAME, None)
    return None, watched, path_fd_info


def _prime_shim_before_parallel() -> str:
    """Warm `_SHIM_CACHE` and resolve any one-time `CREDENTIAL_PROXY_EXEC_URL`
    notice on the main thread before spawning parallel worker threads."""
    if run_kubectl is not _ORIG_RUN_KUBECTL:
        return ""
    client = _shim_client()
    if client is None or not _has_broker_url():
        return ""
    notice_buf = io.StringIO()
    try:
        client.exec_endpoints(err=notice_buf)
    except Exception:  # noqa: BLE001 - run_kubectl reports endpoint errors per call
        return ""
    return notice_buf.getvalue()


def run_kubectl(args: list[str]) -> tuple[int, str, str]:
    """Run kubectl and return (rc, stdout, stderr). Never raises."""
    client = _shim_client()
    if client is not None and _has_broker_url():
        endpoint_err = io.StringIO()
        try:
            endpoint, fallback = client.exec_endpoints(err=endpoint_err)
        except Exception as exc:  # noqa: BLE001 - never raises, as below
            return 1, "", endpoint_err.getvalue() + str(exc)
        if endpoint and endpoint.strip():
            try:
                rc, stdout, stderr = client.execute_captured(
                    endpoint, [KUBECTL, *args], fallback=fallback
                )
            except Exception as exc:  # noqa: BLE001 - never raises, as below
                return 1, "", endpoint_err.getvalue() + str(exc)
            prefix = endpoint_err.getvalue()
            return rc, stdout, (prefix + stderr) if prefix else stderr
    try:
        res = subprocess.run([KUBECTL, *args], capture_output=True, text=True, check=False)
    except OSError as exc:
        return -1, "", str(exc)
    return res.returncode, res.stdout, res.stderr


_ORIG_RUN_KUBECTL = run_kubectl


def warn_lines(stderr: str, *, err: io.TextIOBase | None = None) -> None:
    err_stream = sys.stderr if err is None else err
    for line in stderr.splitlines():
        if line.strip():
            err_stream.write(f"{WARNING_PREFIX}{line}\n")


_ORIG_WARN_LINES = warn_lines


def kubectl_json(args: list[str]) -> dict:
    """Run `kubectl ... -o json` and parse it.

    kubectl asked for several resource types at once still prints the ones it
    could read when one of them fails, exiting non-zero. Parseable output wins
    and the failure is a warning; only unparseable output is an error.
    """
    rc, stdout, stderr = run_kubectl([*args, OUTPUT_FLAG, OUTPUT_JSON])
    return _parse_kubectl_json(args, rc, stdout, stderr)


def _parse_kubectl_json(args: list[str], rc: int, stdout: str, stderr: str) -> dict:
    """kubectl_json's handling of one finished `kubectl ... -o json` call."""
    warn_lines(stderr)
    if not stdout.strip():
        if rc != 0:
            raise RuntimeError(f"kubectl {' '.join(args)} failed ({rc}): {stderr.strip()}")
        return {_K8S_ITEMS_KEY: []}
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"kubectl {' '.join(args)} returned unparseable output ({rc}): {exc}")
    if not isinstance(parsed, dict):
        raise RuntimeError(
            _NON_OBJECT_JSON_ERROR.format(
                args=_ARGS_SEPARATOR.join(args), rc=rc, kind=type(parsed).__name__
            )
        )
    return parsed


_ORIG_KUBECTL_JSON = kubectl_json


def listing_refused(stderr: str) -> bool:
    """Whether kubectl's stderr says the listing is refused for good
    (LISTING_REFUSAL_MARKERS) rather than failed for the moment."""
    return any(marker in (stderr or "") for marker in LISTING_REFUSAL_MARKERS)


def kubectl_names(resource: str, namespace: str) -> set[str] | ListingRefused | None:
    """Names of `resource` in `namespace`; REFUSED when the listing is refused
    for good (Forbidden, a kind the cluster does not serve); None when the
    API cannot be read this time.

    `-o name` carries no object bodies, which is what keeps Secret contents
    out of this script.
    """
    rc, stdout, stderr = run_kubectl(_names_args(resource, namespace))
    return _parse_kubectl_names(resource, namespace, rc, stdout, stderr)


def _names_args(resource: str, namespace: str) -> list[str]:
    return [KUBECTL_GET, resource, NAMESPACE_FLAG, namespace, OUTPUT_FLAG, OUTPUT_NAME]


def _parse_kubectl_names(
    resource: str, namespace: str, rc: int, stdout: str, stderr: str
) -> set[str] | ListingRefused | None:
    """kubectl_names' handling of one finished `kubectl get -o name` call."""
    if rc != 0:
        sys.stderr.write(
            f"{WARNING_PREFIX}cannot list {resource} in {namespace}; references to "
            f"{resource} are not checked: {stderr.strip()}\n"
        )
        return REFUSED if listing_refused(stderr) else None
    return {
        line.split(RESOURCE_NAME_SEPARATOR, 1)[-1] for line in stdout.splitlines() if line.strip()
    }


_ORIG_KUBECTL_NAMES = kubectl_names


def _effective_workers(workers: int) -> int:
    """Clamp worker width into [MIN_COLLECT_WORKERS, MAX_COLLECT_WORKERS]."""
    return max(MIN_COLLECT_WORKERS, min(MAX_COLLECT_WORKERS, workers))


def _parallel_reads() -> bool:
    """Whether kubectl reads may be issued concurrently: only through this
    module's own functions, so a replaced one (a test's fake fed in call
    order) still sees every call in the order it always did."""
    return (
        KUBECTL == _DEFAULT_KUBECTL
        and run_kubectl is _ORIG_RUN_KUBECTL
        and kubectl_json is _ORIG_KUBECTL_JSON
        and kubectl_names is _ORIG_KUBECTL_NAMES
        and subprocess.run is _ORIG_SUBPROCESS_RUN
    )


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

    def __init__(self, lister=None):
        self._lister = kubectl_names if lister is None else lister
        self._cache: dict[tuple[str, str], set[str] | ListingRefused | None] = {}

    def exists(self, kind: str, namespace: str, name: str) -> bool | None:
        """True/False when the kind is resolvable, None when it is not."""
        resource = REFERENT_RESOURCES.get(kind)
        if resource is None:
            return None
        key = (resource, namespace)
        if key not in self._cache:
            self._cache[key] = self._lister(resource, namespace)
        names = self._cache[key]
        if names is None or isinstance(names, ListingRefused):
            return None
        return name in names


_ORIG_NAME_RESOLVER_EXISTS = NameResolver.exists


class ScaleResolver:
    """Answers whether a Gateway's in-cluster proxy is scaled to zero, from
    listings read on first use and cached. A read that fails, or an answer it
    cannot give, counts as not scaled to zero."""

    def __init__(self, reader=None):
        self._reader = kubectl_json if reader is None else reader
        self._cache: dict[tuple[str, ...], list[dict] | None] = {}

    def _items(self, *args: str) -> list[dict] | None:
        if args not in self._cache:
            try:
                listing = self._reader(list(args))
            except RuntimeError as exc:
                sys.stderr.write(f"{WARNING_PREFIX}scaled-to-zero check could not read: {exc}\n")
                listing = None
            if listing is None:
                self._cache[args] = None
            else:
                self._cache[args] = (listing.get("items") or []) if "items" in listing else [listing]
        return self._cache[args]

    def proxy_at_zero(self, gateway: dict) -> bool:
        """Whether the Gateway's in-cluster proxy Deployment exists and every
        one found asks for zero replicas. A namespace that cannot be read
        counts as one holding a running proxy."""
        meta = gateway.get("metadata") or {}
        namespace = meta.get("namespace", "")
        class_name = (gateway.get("spec") or {}).get("gatewayClassName")
        if not class_name:
            return False
        classes = self._items(KUBECTL_GET, GATEWAY_CLASSES_RESOURCE, class_name)
        if not classes:
            return False
        controller = (classes[0].get("spec") or {}).get("controllerName") or ""
        template, extra_namespaces = PROXY_SELECTORS.get(controller, DEFAULT_PROXY_SELECTOR)
        selector = template.format(name=meta.get("name", ""), namespace=namespace)
        proxies: list[dict] = []
        for where in dict.fromkeys((namespace, *extra_namespaces)):
            found = self._items(KUBECTL_GET, DEPLOYMENTS_RESOURCE, LABEL_SELECTOR_FLAG, selector, NAMESPACE_FLAG, where)
            if found is None:
                return False
            proxies.extend(found)
        return bool(proxies) and all((d.get("spec") or {}).get(REPLICAS_SPEC_KEY) == 0 for d in proxies)


# --------------------------------------------------------------------------
# time helpers
# --------------------------------------------------------------------------


@functools.lru_cache(maxsize=_PARSE_TIME_CACHE_MAX)
def _parse_time_str(value: str) -> datetime | None:
    if not value:
        return None
    text = value[:-1] + UTC_OFFSET if value.endswith(ZULU_SUFFIX) else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_time(value) -> datetime | None:
    """Parse a Kubernetes RFC 3339 timestamp; None when absent or malformed."""
    if not value or not isinstance(value, str):
        return None
    return _parse_time_str(value)


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


def is_exempt(obj: dict) -> bool:
    annotations = (obj.get("metadata") or {}).get("annotations")
    return isinstance(annotations, dict) and annotations.get(EXEMPT_ANNOTATION) == EXEMPT_VALUE


def finding(obj: dict, heuristic: str, detail: str, stalled: timedelta) -> dict:
    seconds = int(stalled.total_seconds())
    return {
        "object": object_label(obj),
        "namespace": (obj.get("metadata") or {}).get("namespace", ""),
        "heuristic": heuristic,
        "detail": detail,
        "stalled_for": format_duration(seconds),
        "stalled_seconds": seconds,
        EXEMPT_FIELD: is_exempt(obj),
    }


def ready_reason(obj: dict) -> str:
    """The reason on the object's own Ready condition while it is not True,
    else the empty string."""
    status = obj.get("status")
    if not isinstance(status, dict):
        return ""
    for path, cond in iter_conditions(status):
        if path == "" and cond.get("type") == READY_CONDITION_TYPE and cond.get("status") != CONDITION_TRUE:
            return str(cond.get("reason") or "")
    return ""


def ready_in_progress(obj: dict) -> bool:
    """Whether the object's own Ready condition says its controller is still
    applying or deleting it (IN_PROGRESS_READY_REASONS), in which case no
    heuristic calls it stalled."""
    return ready_reason(obj) in IN_PROGRESS_READY_REASONS


def dependency_wait(reason: str, threshold: timedelta) -> timedelta:
    """The age a Ready=False with this reason must reach: the usual threshold,
    or DEPENDENCY_STALL_MINUTES at least for DependencyNotReady, by every
    heuristic alike, so the generation lag of an object waiting on a
    dependency is not a row sooner than its condition."""
    if reason == DEPENDENCY_NOT_READY_REASON:
        return max(threshold, timedelta(minutes=DEPENDENCY_STALL_MINUTES))
    return threshold


def check_generation_lag(obj: dict, now: datetime, threshold: timedelta) -> list[dict]:
    meta = obj.get("metadata") or {}
    status = obj.get("status")
    if not isinstance(status, dict) or "observedGeneration" not in status:
        return []
    if ready_in_progress(obj):
        return []
    threshold = dependency_wait(ready_reason(obj), threshold)
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
        reason = cond.get("reason") or ""
        if ctype == READY_CONDITION_TYPE and reason in IN_PROGRESS_READY_REASONS:
            continue
        wait = dependency_wait(reason, threshold) if ctype == READY_CONDITION_TYPE else threshold
        if age < wait:
            continue
        where = f"{path} " if path else ""
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


def is_scaled_to_zero_row(row: dict) -> bool:
    """The Gateway's own Programmed=False stale condition: not a listener's,
    which carries a listeners[...] prefix."""
    return row["heuristic"] == HEURISTIC_CONDITION and row["detail"].split(" ", 1)[0] == SCALED_TO_ZERO_DETAIL


def analyze(
    objects: list[dict],
    events: list[dict],
    resolver: NameResolver,
    now: datetime,
    override_minutes: int | None = None,
    scale: ScaleResolver | None = None,
    workers: int = DEFAULT_COLLECT_WORKERS,
) -> list[dict]:
    """Apply every heuristic to every object; pure apart from the resolvers."""
    prefetch_references(resolver, objects, now, override_minutes, workers=workers)
    events_by_object = index_events(events)
    scale = scale or ScaleResolver()
    findings: list[dict] = []
    for obj in objects:
        kind = obj.get("kind", "")
        if kind in SKIPPED_OBJECT_KINDS or is_terminal_pod(obj) or is_finished(obj):
            continue
        name = (obj.get("metadata") or {}).get("name", "")
        threshold = threshold_for(kind, override_minutes)
        found = [
            *check_generation_lag(obj, now, threshold),
            *check_stale_conditions(obj, now, threshold),
            *check_repeating_warnings(obj, events_by_object.get((kind, name), []), now, threshold),
            *check_dangling_references(obj, resolver, now, threshold),
        ]
        if kind == GATEWAY_KIND and any(is_scaled_to_zero_row(f) for f in found) and scale.proxy_at_zero(obj):
            found = [f for f in found if not is_scaled_to_zero_row(f)]
        findings.extend(found)
    findings.sort(key=lambda f: (-f["stalled_seconds"], f["object"], f["heuristic"]))
    return findings


def stalled_object_count(findings: list[dict]) -> int:
    return len({(f["namespace"], f["object"]) for f in findings})


def prefetch_references(
    resolver,
    objects: list[dict],
    now: datetime,
    override_minutes: int | None = None,
    workers: int = DEFAULT_COLLECT_WORKERS,
) -> None:
    """Read, concurrently, the `-o name` listings analyze() will look up.

    Exactly the (resource, namespace) pairs check_dangling_references asks
    the resolver about, in the order it first asks, so the listings and any
    warnings are the ones a sequential analyze() reads. A resolver that is not
    this module's NameResolver over kubectl_names is left alone.
    """
    worker_limit = _effective_workers(workers)
    if (
        worker_limit <= MIN_COLLECT_WORKERS
        or type(resolver) is not NameResolver
        or resolver._lister is not _ORIG_KUBECTL_NAMES
        or NameResolver.exists is not _ORIG_NAME_RESOLVER_EXISTS
        or _NAME_RESOLVER_EXISTS_ATTR in resolver.__dict__
        or not _parallel_reads()
    ):
        return
    keys: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set(resolver._cache)
    for obj in objects:
        kind = obj.get(_K8S_KIND_KEY, "")
        if kind in SKIPPED_OBJECT_KINDS or is_terminal_pod(obj) or is_finished(obj):
            continue
        spec = obj.get(_K8S_SPEC_KEY)
        if not isinstance(spec, dict):
            continue
        written = spec_write_time(obj)
        if written is None or now - written < threshold_for(kind, override_minutes):
            continue
        own_namespace = (obj.get(_K8S_METADATA_KEY) or {}).get(_K8S_NAMESPACE_KEY, "")
        for _path, ref_kind, _name, namespace, optional in iter_references(spec):
            if optional:
                continue
            resource = REFERENT_RESOURCES.get(ref_kind)
            if resource is None:
                continue
            key = (resource, namespace or own_namespace)
            if key not in seen:
                seen.add(key)
                keys.append(key)
    if not keys:
        return
    if len(keys) == 1:
        resource, namespace = keys[0]
        rc, stdout, stderr = run_kubectl(_names_args(resource, namespace))
        resolver._cache[(resource, namespace)] = _parse_kubectl_names(
            resource, namespace, rc, stdout, stderr
        )
        return
    notice = _prime_shim_before_parallel()
    if notice:
        warn_lines(notice)
    with ThreadPoolExecutor(max_workers=min(worker_limit, len(keys))) as pool:
        results = list(pool.map(lambda key: run_kubectl(_names_args(*key)), keys))
    for (resource, namespace), (rc, stdout, stderr) in zip(keys, results):
        resolver._cache[(resource, namespace)] = _parse_kubectl_names(
            resource, namespace, rc, stdout, stderr
        )


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


def collect(
    namespace: str, kinds: str | None, workers: int = DEFAULT_COLLECT_WORKERS
) -> tuple[list[dict], list[dict]]:
    """Objects of every requested kind in the namespace, and its Warning events.

    One kubectl call per kind, not one for the namespace. In the Cluster
    Agent's shell, kubectl is the credential-proxy shim: the broker cuts a
    reply at its output cap (CREDENTIAL_PROXY_MAX_OUTPUT_BYTES, 8 MiB as the
    operator deploys it; the broker's own default is 4 MiB) and hands back the
    prefix with kubectl's own exit code, so a busy
    namespace read in one call arrives as JSON cut mid-string. Read per kind,
    an overflow costs that one kind, and a warning names it; only a scan that
    could read no kind at all is an error.
    """
    resources = [r for r in (kinds.split(",") if kinds else namespaced_resources()) if r]
    if not resources:
        return [], []
    events_args = [
        KUBECTL_GET,
        EVENTS_RESOURCE,
        NAMESPACE_FLAG,
        namespace,
        FIELD_SELECTOR_FLAG,
        WARNING_EVENTS_SELECTOR,
    ]
    worker_limit = _effective_workers(workers)
    if worker_limit <= MIN_COLLECT_WORKERS or not _parallel_reads():
        return _collect_listings(namespace, resources, events_args, kubectl_json, kubectl_json)
    # The reads are independent: issue them together, then handle each result
    # in the order the sequential scan reads them, so warnings, errors and
    # object order are exactly what it produces.
    notice = _prime_shim_before_parallel()
    if notice:
        warn_lines(notice)
    with ThreadPoolExecutor(max_workers=min(worker_limit, len(resources) + 1)) as pool:
        listing_futures = deque(
            pool.submit(run_kubectl, [*_listing_args(resource, namespace), OUTPUT_FLAG, OUTPUT_JSON])
            for resource in resources
        )
        events_future = pool.submit(run_kubectl, [*events_args, OUTPUT_FLAG, OUTPUT_JSON])

        def read_listing(args: list[str]) -> dict:
            return _parse_kubectl_json(args, *listing_futures.popleft().result())

        def read_events(args: list[str]) -> dict:
            return _parse_kubectl_json(args, *events_future.result())

        return _collect_listings(namespace, resources, events_args, read_listing, read_events)


def _listing_args(resource: str, namespace: str) -> list[str]:
    return [KUBECTL_GET, resource, NAMESPACE_FLAG, namespace, SHOW_MANAGED_FIELDS_FLAG]


def _collect_listings(namespace, resources, events_args, read_listing, read_events):
    objects: list[dict] = []
    unread: list[str] = []
    for resource in resources:
        try:
            listing = read_listing(_listing_args(resource, namespace))
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
        events = read_events(events_args).get(_K8S_ITEMS_KEY)
    except RuntimeError as exc:
        sys.stderr.write(
            f"{WARNING_PREFIX}events in {namespace} not read; {HEURISTIC_EVENTS} is not "
            f"checked: {exc}\n"
        )
        events = []
    return objects, list(events or [])


# --------------------------------------------------------------------------
# cluster mode
# --------------------------------------------------------------------------


def reply_cut(rc: int, stderr: str) -> bool:
    """Whether the credential proxy cut this reply at its output cap."""
    return PROXY_TRUNCATED_TEXT in (stderr or "") or rc == EXIT_OUTPUT_TRUNCATED


def by_namespace(items: list[dict]) -> dict[str, list[dict]]:
    split: dict[str, list[dict]] = {}
    for item in items:
        if isinstance(item, dict):
            split.setdefault(str((item.get("metadata") or {}).get("namespace", "")), []).append(item)
    return split


def raw_pages(
    collection: tuple[str, str, str],
    field_selector: str | None = None,
    *,
    err: io.TextIOBase | None = None,
) -> list[dict] | None:
    """Every object of one raw collection, `PAGE_SIZE` a page, each item
    stamped with its kind and apiVersion; None, with a warning, when a page
    failed, could not be parsed, or was cut at the smallest page this script
    asks for. A cut page is asked for again at half the size: the continue
    token names where the page starts, not how long it is."""
    err_stream = sys.stderr if err is None else err
    path, kind, api_version = collection
    items: list[dict] = []
    token = ""
    size = PAGE_SIZE
    seen_tokens: set[str] = set()
    for _ in range(RAW_MAX_PAGES):
        params = {RAW_LIMIT_KEY: str(size)}
        if field_selector:
            params[RAW_FIELD_SELECTOR_KEY] = field_selector
        if token:
            params[RAW_CONTINUE_KEY] = token
        url = f"{path}{RAW_QUERY_MARK}{urllib.parse.urlencode(params)}"
        rc, stdout, stderr = run_kubectl([KUBECTL_GET, RAW_FLAG, url])
        if reply_cut(rc, stderr):
            if size // RAW_PAGE_HALVING >= RAW_PAGE_SIZE_MIN:
                size //= RAW_PAGE_HALVING
                continue
            if err is None:
                warn_lines(stderr)
            else:
                warn_lines(stderr, err=err_stream)
            err_stream.write(f"{WARNING_PREFIX}{path} cut at a page of {size}; read per namespace instead\n")
            return None
        if rc != 0 or not stdout.strip():
            if err is None:
                warn_lines(stderr)
            else:
                warn_lines(stderr, err=err_stream)
            err_stream.write(f"{WARNING_PREFIX}{path} page failed ({rc}); read per namespace instead\n")
            return None
        try:
            page = json.loads(stdout)
        except json.JSONDecodeError as exc:
            err_stream.write(f"{WARNING_PREFIX}{path} page is not JSON ({exc}); read per namespace instead\n")
            return None
        page_items = page.get("items") if isinstance(page, dict) else None
        if not isinstance(page_items, list):
            err_stream.write(f"{WARNING_PREFIX}{path} page has no items list; read per namespace instead\n")
            return None
        for item in page_items:
            if isinstance(item, dict):
                item.setdefault("kind", kind)
                item.setdefault("apiVersion", api_version)
        items.extend(page_items)
        metadata = page.get("metadata")
        token = (metadata.get(RAW_CONTINUE_KEY) if isinstance(metadata, dict) else "") or ""
        if not token:
            return items
        if token in seen_tokens:
            err_stream.write(f"{WARNING_PREFIX}{path} repeated a continue token; read per namespace instead\n")
            return None
        seen_tokens.add(token)
    err_stream.write(f"{WARNING_PREFIX}{path} ran past {RAW_MAX_PAGES} pages; read per namespace instead\n")
    return None


_ORIG_RAW_PAGES = raw_pages


def chunked_listing(args: list[str], *, err: io.TextIOBase | None = None) -> list[dict] | None:
    """One `kubectl get ... --all-namespaces --chunk-size=N -o json` reply's
    items; None, with a warning, when the broker cut the reply or the read
    failed. Partial output of a failed read is not kept: across every
    namespace, the objects past the failure would read as absent."""
    err_stream = sys.stderr if err is None else err
    rc, stdout, stderr = run_kubectl([*args, OUTPUT_FLAG, OUTPUT_JSON])
    if reply_cut(rc, stderr):
        if err is None:
            warn_lines(stderr)
        else:
            warn_lines(stderr, err=err_stream)
        err_stream.write(f"{WARNING_PREFIX}{args[1]} across every namespace was cut; read per namespace instead\n")
        return None
    if rc != 0 or not stdout.strip():
        if err is None:
            warn_lines(stderr)
        else:
            warn_lines(stderr, err=err_stream)
        err_stream.write(f"{WARNING_PREFIX}{args[1]} across every namespace failed ({rc}); read per namespace instead\n")
        return None
    try:
        listing = json.loads(stdout)
    except json.JSONDecodeError as exc:
        err_stream.write(f"{WARNING_PREFIX}{args[1]} across every namespace returned unparseable output ({exc}); read per namespace instead\n")
        return None
    return list(listing.get("items") or []) if isinstance(listing, dict) else None


_ORIG_CHUNKED_LISTING = chunked_listing


def read_kind_cluster_wide(
    resource: str, *, err: io.TextIOBase | None = None
) -> dict[str, list[dict]] | None:
    """Every object of one kind, by namespace; None when the kind is left to
    the per-namespace read: a `kind/name` (one object, in one namespace), a
    raw collection whose pages could not all be read, or a chunked read the
    broker cut or that failed."""
    if RESOURCE_NAME_SEPARATOR in resource:
        return None
    collection = RAW_COLLECTIONS.get(resource)
    if collection is not None:
        items = raw_pages(collection) if err is None else raw_pages(collection, err=err)
        return None if items is None else by_namespace(items)
    args = [KUBECTL_GET, resource, ALL_NAMESPACES_FLAG, CHUNK_SIZE_ARG, SHOW_MANAGED_FIELDS_FLAG]
    items = chunked_listing(args) if err is None else chunked_listing(args, err=err)
    return None if items is None else by_namespace(items)


_ORIG_READ_KIND_CLUSTER_WIDE = read_kind_cluster_wide


def read_warning_events_cluster_wide(*, err: io.TextIOBase | None = None) -> dict[str, list[dict]] | None:
    """Every Warning event, by namespace: the raw collection paged, else one
    chunked read; None when neither could read them whole."""
    items = (
        raw_pages(EVENTS_COLLECTION, WARNING_EVENTS_SELECTOR)
        if err is None
        else raw_pages(EVENTS_COLLECTION, WARNING_EVENTS_SELECTOR, err=err)
    )
    if items is None:
        args = [KUBECTL_GET, EVENTS_RESOURCE, ALL_NAMESPACES_FLAG, CHUNK_SIZE_ARG, FIELD_SELECTOR_FLAG, WARNING_EVENTS_SELECTOR]
        items = chunked_listing(args) if err is None else chunked_listing(args, err=err)
    return None if items is None else by_namespace(items)


_ORIG_READ_WARNING_EVENTS_CLUSTER_WIDE = read_warning_events_cluster_wide


def _parallel_cluster_reads() -> bool:
    return (
        _parallel_reads()
        and warn_lines is _ORIG_WARN_LINES
        and raw_pages is _ORIG_RAW_PAGES
        and chunked_listing is _ORIG_CHUNKED_LISTING
        and read_kind_cluster_wide is _ORIG_READ_KIND_CLUSTER_WIDE
        and read_warning_events_cluster_wide is _ORIG_READ_WARNING_EVENTS_CLUSTER_WIDE
    )


def kubectl_names_cluster_wide(resource: str) -> set[tuple[str, str]] | None:
    """The (namespace, name) pairs of `resource` across every namespace, or
    None when the API cannot be read or the reply was cut, since a cut
    listing would report every reference past the cut as dangling. Names
    only, as the per-namespace `-o name` is, so Secret bodies never cross."""
    rc, stdout, stderr = run_kubectl([KUBECTL_GET, resource, ALL_NAMESPACES_FLAG, CHUNK_SIZE_ARG, NO_HEADERS_FLAG, OUTPUT_FLAG, NAME_COLUMNS])
    if rc != 0 or reply_cut(rc, stderr):
        sys.stderr.write(
            f"{WARNING_PREFIX}cannot list {resource} across every namespace; references to "
            f"{resource} are resolved per namespace instead: {stderr.strip()}\n"
        )
        return None
    names: set[tuple[str, str]] = set()
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) >= NAME_COLUMN_COUNT:
            names.add((parts[0], parts[1]))
    return names


class ClusterNameResolver(NameResolver):
    """`NameResolver` over one listing per referent kind across every
    namespace, so a cluster of hundreds of namespaces resolves its references
    from at most one read per kind. A kind whose cluster-wide listing failed
    or was cut is resolved per namespace, through `NameResolver`'s own
    listing, as the namespace read resolves it: the cluster-wide read saves
    calls and never leaves a reference unchecked that the namespace read
    would have checked, so a dangling-reference row does not clear on a
    listing that failed. A kind the per-namespace listing refuses outright
    (REFUSED: Forbidden, or not served) stays unresolved, as before, and is
    not asked for again: Secrets are refused at both scopes on the read-only
    permission set (the admission policy lets no agent Role grant them), and
    a Secret reference is the common one, so without that memory every
    namespace with one would cost a refused call, the per-namespace cost the
    cluster read exists to remove. A per-namespace listing that fails for the
    moment (a timeout, a 5xx, a cut reply: None) leaves that one namespace's
    references unchecked, with the warning, and the kind is asked for again
    in the next namespace: one namespace's bad moment must not clear a
    dangling-reference row in another."""

    def __init__(self, lister=None, namespace_lister=None):
        super().__init__(lister=namespace_lister)
        self._cluster_lister = kubectl_names_cluster_wide if lister is None else lister
        self._names: dict[str, set[tuple[str, str]] | None] = {}
        self._unlistable: set[str] = set()

    def exists(self, kind: str, namespace: str, name: str) -> bool | None:
        resource = REFERENT_RESOURCES.get(kind)
        if resource is None:
            return None
        if resource not in self._names:
            self._names[resource] = self._cluster_lister(resource)
        names = self._names[resource]
        if names is not None:
            return (namespace, name) in names
        if resource in self._unlistable:
            return None
        answer = super().exists(kind, namespace, name)
        if isinstance(self._cache.get((resource, namespace)), ListingRefused):
            self._unlistable.add(resource)
        return answer


def collect_cluster(
    kinds: list[str], workers: int = DEFAULT_COLLECT_WORKERS
) -> tuple[dict[str, dict[str, list[dict]]], dict[str, list[dict]] | None, list[str]]:
    """Every object of every kind in `kinds`, by kind and namespace, the
    Warning events by namespace (None when they could not be read whole),
    and the kinds left to the per-namespace read."""
    objects: dict[str, dict[str, list[dict]]] = {}
    fallback: list[str] = []
    worker_limit = _effective_workers(workers)
    if not kinds or worker_limit <= MIN_COLLECT_WORKERS or not _parallel_cluster_reads():
        for resource in kinds:
            split = read_kind_cluster_wide(resource)
            if split is None:
                fallback.append(resource)
            else:
                objects[resource] = split
        return objects, read_warning_events_cluster_wide(), fallback

    def read_kind_buffered(resource: str) -> tuple[dict[str, list[dict]] | None, str]:
        buf = io.StringIO()
        return read_kind_cluster_wide(resource, err=buf), buf.getvalue()

    def read_events_buffered() -> tuple[dict[str, list[dict]] | None, str]:
        buf = io.StringIO()
        return read_warning_events_cluster_wide(err=buf), buf.getvalue()

    # The cluster-wide kind reads and Warning events read are independent:
    # issue them concurrently, buffering each worker's warnings so stderr,
    # objects, and fallback match the sequential scan in exact order.
    notice = _prime_shim_before_parallel()
    if notice:
        warn_lines(notice)
    with ThreadPoolExecutor(max_workers=min(worker_limit, len(kinds) + 1)) as pool:
        kind_futures = [pool.submit(read_kind_buffered, resource) for resource in kinds]
        events_future = pool.submit(read_events_buffered)
        for resource, future in zip(kinds, kind_futures):
            split, warnings_text = future.result()
            if warnings_text:
                sys.stderr.write(warnings_text)
            if split is None:
                fallback.append(resource)
            else:
                objects[resource] = split
        events, events_warnings = events_future.result()
        if events_warnings:
            sys.stderr.write(events_warnings)
    return objects, events, fallback


def parse_extra_kinds(values: list[str]) -> dict[str, list[str]]:
    """`--extra-kinds <namespace>=<kinds>` values as {namespace: [kinds]}."""
    extra: dict[str, list[str]] = {}
    for value in values or []:
        namespace, sep, kinds = value.partition(EXTRA_KINDS_SEPARATOR)
        if not sep or not namespace:
            raise ValueError(f"--extra-kinds takes <namespace>=<kinds>, got {value!r}")
        extra.setdefault(namespace, []).extend(k for k in kinds.split(KINDS_SEPARATOR) if k)
    return extra


def report_cluster(
    namespaces: list[str],
    kinds: str | None,
    extra: dict[str, list[str]],
    now: datetime,
    override_minutes: int | None = None,
    resolver: NameResolver | None = None,
    scale: ScaleResolver | None = None,
    workers: int = DEFAULT_COLLECT_WORKERS,
) -> dict:
    """The cluster report: per named namespace, the report the namespace
    read gives for the kinds asked there (`kinds`, every namespaced kind
    when None, plus the namespace's `extra`), from one cluster-wide read per
    kind, and the kinds and events left to the per-namespace read."""
    # None is every namespaced kind; an empty string is no kind, for a
    # namespace list whose kinds are all in `extra`.
    base = [r for r in (kinds.split(KINDS_SEPARATOR) if kinds is not None else namespaced_resources()) if r]
    asked = {ns: list(dict.fromkeys(base + list(extra.get(ns) or []))) for ns in namespaces}
    every = list(dict.fromkeys(r for ns in namespaces for r in asked[ns]))
    objects, events, fallback = collect_cluster(every, workers=workers)
    resolver = resolver or ClusterNameResolver()
    scale = scale or ScaleResolver()
    reports: dict[str, dict] = {}
    for namespace in namespaces:
        # The objects in the order the per-namespace read lists them: kind
        # by kind as asked, each kind in the order the server gave.
        here = [o for r in asked[namespace] if r in objects for o in objects[r].get(namespace, [])]
        findings = analyze(
            here,
            events.get(namespace, []) if events is not None else [],
            resolver,
            now,
            override_minutes,
            scale,
            workers=workers,
        )
        reports[namespace] = {REPORT_NAMESPACE_KEY: namespace, REPORT_COUNT_KEY: stalled_object_count(findings), REPORT_FINDINGS_KEY: findings}
    return {REPORT_NAMESPACES_KEY: reports, REPORT_FALLBACK_KEY: fallback, REPORT_EVENTS_FALLBACK_KEY: events is None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--namespace", "-n", help="namespace to inspect")
    target.add_argument(
        "--namespaces",
        help="cluster mode: comma-separated namespaces to report on, each kind read once across "
        "every namespace and split per namespace; prints one JSON report per namespace (needs --json)",
    )
    parser.add_argument(
        "--kind",
        help="comma-separated kubectl resource names to inspect instead of every "
        "namespaced kind (e.g. deployments,gateways.gateway.networking.k8s.io)",
    )
    parser.add_argument(
        "--extra-kinds",
        action="append",
        metavar="NAMESPACE=KINDS",
        help="cluster mode: kinds one namespace is asked about beyond --kind; repeatable",
    )
    parser.add_argument(
        "--threshold-minutes",
        type=int,
        help=f"stall age for every kind, replacing the default of "
        f"{DEFAULT_STALL_MINUTES} and the per-kind table {STALL_MINUTES_BY_KIND}",
    )
    parser.add_argument(
        WORKERS_FLAG,
        type=int,
        default=DEFAULT_COLLECT_WORKERS,
        help=_WORKERS_HELP,
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args(argv)
    if args.workers < MIN_COLLECT_WORKERS:
        parser.error(_WORKERS_MIN_ERROR)
    workers = _effective_workers(args.workers)

    if args.namespaces is not None:
        if not args.json:
            parser.error("--namespaces prints one JSON report per namespace; pass --json")
        namespaces = [n for n in args.namespaces.split(NAMESPACES_SEPARATOR) if n]
        if not namespaces:
            parser.error("--namespaces names no namespace")
        try:
            extra = parse_extra_kinds(args.extra_kinds)
            report = report_cluster(
                namespaces,
                args.kind,
                extra,
                datetime.now(timezone.utc),
                args.threshold_minutes,
                workers=workers,
            )
        except (RuntimeError, ValueError) as exc:
            sys.stderr.write(f"error: {exc}\n")
            return EXIT_ERROR
        print(json.dumps(report, indent=2))
        return EXIT_OK
    if args.extra_kinds:
        parser.error("--extra-kinds goes with --namespaces")

    try:
        objects, events = collect(args.namespace, args.kind, workers=workers)
    except RuntimeError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return EXIT_ERROR

    findings = analyze(
        objects,
        events,
        NameResolver(),
        datetime.now(timezone.utc),
        args.threshold_minutes,
        workers=workers,
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
