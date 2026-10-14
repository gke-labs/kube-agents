#!/usr/bin/env python3
"""What the credential proxy relays for the gateway's watchers.

The k8s-event-watcher and the drift-detector run in the gateway pod's
`agent-api-auth` container. They used to call Google on the pod's Workload
Identity: the GKE API to find each fleet cluster, a Google bearer on each fleet
API server, and Pub/Sub for the drift detector's audit records. They now make
those calls through the broker, on the broker's credential, under the
`watcher` role, and this module is the whole of what decides which calls the
broker makes for them.

Two tables, one per route family:

- `/v1/watcher/gcp/<host>/<path>`: the GKE `clusters.get` the watchers use to
  learn a cluster's Autopilot bit, and the four Pub/Sub calls the drift detector
  makes, each pinned to the one subscription the operator configured.
- `/v1/k8s/<project>/<location>/<cluster>/<path>`: GET only. List and watch of
  core/v1 events across the cluster (the event watcher), and a GET of one named
  object of any kind but `secrets` (the drift detector's join reads the object
  an audit record names). No other list, no watch of anything else (the
  deprecated `watch/<resource>` paths included), no subresource.

Standard library only, like `api_policy`. `Decision` is reused from
`command_policy`. No refusal message repeats caller text.
"""

from __future__ import annotations

import json
import re
from typing import Any

from command_policy import Decision

WATCHER_GCP_PREFIX = "/v1/watcher/gcp/"
WATCHER_K8S_PREFIX = "/v1/k8s/"

METHOD_GET = "GET"
METHOD_POST = "POST"

GKE_HOST = "container.googleapis.com"
PUBSUB_HOST = "pubsub.googleapis.com"

# Rule ids, in the order the evaluators answer.
RULE_GCP_METHOD = "gcp.watcher.method"
RULE_GCP_ROUTE = "gcp.watcher.route"
RULE_GCP_SUBSCRIPTION = "gcp.watcher.subscription"
RULE_GCP_BODY = "gcp.watcher.body"
RULE_K8S_METHOD = "kubernetes.watcher.method"
RULE_K8S_TARGET = "kubernetes.watcher.target"
RULE_K8S_PATH = "kubernetes.watcher.path"
RULE_K8S_SECRETS = "kubernetes.watcher.secrets"
RULE_K8S_LEGACY = "kubernetes.watcher.legacy-path"
RULE_K8S_QUERY = "kubernetes.watcher.query"

# Google's project id grammar, and GKE's location and cluster name grammars:
# lower-case, digits and hyphens, so no segment can smuggle a slash or a dot.
PROJECT_ID = r"[a-z][a-z0-9-]{4,28}[a-z0-9]"
# A domain-scoped project carries `<domain>:` in front of its id.
PROJECT = rf"(?:[a-z0-9][a-z0-9.-]*[a-z0-9]:)?{PROJECT_ID}"
# A Pub/Sub path may name the project by id or by number.
PUBSUB_PROJECT = rf"(?:{PROJECT}|[0-9]{{1,20}})"
LOCATION = r"[a-z][a-z0-9-]{1,62}"
CLUSTER = r"[a-z]([a-z0-9-]{0,38}[a-z0-9])?"
# A Pub/Sub subscription id: 3 to 255 characters, starting with a letter.
SUBSCRIPTION = r"[A-Za-z][A-Za-z0-9._~+%-]{2,254}"

_GKE_CLUSTER_GET = re.compile(rf"v1/projects/{PROJECT}/locations/{LOCATION}/clusters/{CLUSTER}")
_PUBSUB_SUBSCRIPTION = re.compile(
    rf"v1/(?P<sub>projects/{PUBSUB_PROJECT}/subscriptions/{SUBSCRIPTION})(?P<verb>:pull|:acknowledge|:modifyAckDeadline)?"
)
# The body keys each Pub/Sub POST may carry, and the type of each.
PUBSUB_BODIES: dict[str, dict[str, type]] = {
    ":pull": {"maxMessages": int, "returnImmediately": bool},
    ":acknowledge": {"ackIds": list},
    ":modifyAckDeadline": {"ackIds": list, "ackDeadlineSeconds": int},
}
_TARGET = re.compile(rf"(?P<project>{PROJECT})/(?P<location>{LOCATION})/(?P<cluster>{CLUSTER})")

# A Kubernetes path segment for a namespace, group, version or resource plural:
# DNS-1123 labels and subdomains. Nothing here admits a dot segment or an encoding.
SEGMENT = r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?"
# An object name is looser: most kinds hold names to DNS-1123, but RBAC names are
# path-segment names (`system:aggregate-to-edit`, `MyBinding`). No slash, no
# percent-encoding, and `.` and `..` refused separately.
NAME = r"[A-Za-z0-9._:@+~=-]{1,253}"
DOT_SEGMENTS = frozenset({".", ".."})
_EVENTS_LIST = re.compile(r"api/v1/events")
_NAMED_CORE = re.compile(rf"api/v1/(namespaces/(?P<ns>{SEGMENT})/)?(?P<resource>{SEGMENT})/(?P<name>{NAME})")
_NAMED_GROUP = re.compile(
    rf"apis/(?P<group>{SEGMENT})/(?P<version>{SEGMENT})/(namespaces/(?P<ns>{SEGMENT})/)?(?P<resource>{SEGMENT})/(?P<name>{NAME})"
)
# `namespaces/<ns>` alone matches the cluster-scoped named form with resource
# "namespaces"; that is a named GET of a Namespace object and is allowed.
REFUSED_RESOURCES = frozenset({"secrets"})
# Segments the API server serves in the resource position that are not a
# resource: the deprecated `watch/<resource>` prefix (a watch, of any kind,
# Secrets included) and `proxy/`. Read as a named GET they would be a watch the
# events rule never saw.
REFUSED_PATH_SEGMENTS = frozenset({"watch", "proxy"})
# Query keys an events list or watch may carry: what client-go's reflector sends.
# `timeout` is the client-side request timeout client-go adds to a watch beside
# the server-side `timeoutSeconds` (seen on a live watch-list request:
# `...&timeout=5m49s&timeoutSeconds=349&watch=true`); without it every watch the
# real watcher opens is refused and it lists once and never watches.
EVENTS_QUERY_KEYS = frozenset(
    {
        "allowWatchBookmarks",
        "continue",
        "fieldSelector",
        "labelSelector",
        "limit",
        "resourceVersion",
        "resourceVersionMatch",
        "sendInitialEvents",
        "timeout",
        "timeoutSeconds",
        "watch",
    }
)
# A named GET may only ask for a resource version.
NAMED_QUERY_KEYS = frozenset({"resourceVersion"})


def _refuse(rule_id: str, message: str) -> Decision:
    return Decision(allowed=False, rule_id=rule_id, message=message)


def evaluate_gcp(method: str, host: str, path: str, body: bytes, subscription: str) -> Decision:
    """Allow or refuse one `/v1/watcher/gcp/` call.

    `subscription` is the configured `projects/<p>/subscriptions/<s>`; empty
    means the drift detector is off and every Pub/Sub call is refused.
    """
    if method not in (METHOD_GET, METHOD_POST):
        return _refuse(RULE_GCP_METHOD, "The watcher relay forwards GET and POST only.")
    if host == GKE_HOST:
        if method == METHOD_GET and _GKE_CLUSTER_GET.fullmatch(path):
            return Decision(allowed=True, rule_id="gcp.watcher.cluster-get", message="")
        return _refuse(RULE_GCP_ROUTE, "The watcher relay reads one GKE cluster by name and nothing else.")
    if host == PUBSUB_HOST:
        match = _PUBSUB_SUBSCRIPTION.fullmatch(path)
        if match is None:
            return _refuse(RULE_GCP_ROUTE, "The watcher relay makes four Pub/Sub calls on one subscription.")
        if not subscription or match.group("sub") != subscription:
            return _refuse(
                RULE_GCP_SUBSCRIPTION,
                "The watcher relay reaches only the drift detector's configured subscription.",
            )
        verb = match.group("verb")
        if verb is None:
            if method != METHOD_GET:
                return _refuse(RULE_GCP_METHOD, "A subscription is read with GET.")
            return Decision(allowed=True, rule_id="gcp.watcher.subscription-get", message="")
        if method != METHOD_POST or not _body_fits(body, PUBSUB_BODIES[verb]):
            return _refuse(RULE_GCP_BODY, "That Pub/Sub call carries a body this relay does not forward.")
        return Decision(allowed=True, rule_id="gcp.watcher.subscription" + verb.replace(":", "-"), message="")
    return _refuse(RULE_GCP_ROUTE, "The watcher relay reaches the GKE and Pub/Sub APIs only.")


def _body_fits(body: bytes, keys: dict[str, type]) -> bool:
    """True when `body` is one JSON object using only `keys`, each of its type."""
    try:
        value = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError, RecursionError):
        return False
    if not isinstance(value, dict):
        return False
    for key, item in value.items():
        wanted = keys.get(key)
        # bool is an int in Python; a count must not be a bool.
        if wanted is None or not isinstance(item, wanted) or (wanted is int and isinstance(item, bool)):
            return False
        if wanted is list and not all(isinstance(entry, str) for entry in item):
            return False
    return True


def split_k8s_target(rest: str) -> tuple[dict[str, str], str] | None:
    """`(target, k8s_path)` from what follows `/v1/k8s/`, or None if malformed."""
    parts = rest.split("/", 3)
    if len(parts) != 4:
        return None
    match = _TARGET.fullmatch("/".join(parts[:3]))
    if match is None:
        return None
    return match.groupdict(), parts[3]


def evaluate_k8s(method: str, rest: str, query: dict[str, list[str]]) -> Decision:
    """Allow or refuse one `/v1/k8s/` call. `rest` is the path after the prefix."""
    if method != METHOD_GET:
        return _refuse(RULE_K8S_METHOD, "The watcher's Kubernetes route is read-only: GET only.")
    split = split_k8s_target(rest)
    if split is None:
        return _refuse(RULE_K8S_TARGET, "The route names a project, a location and a cluster, then a path.")
    _, path = split
    if _EVENTS_LIST.fullmatch(path):
        if not set(query) <= EVENTS_QUERY_KEYS:
            return _refuse(RULE_K8S_QUERY, "An events list or watch carries a query key this route does not forward.")
        return Decision(allowed=True, rule_id="kubernetes.watcher.events", message="")
    match = _NAMED_CORE.fullmatch(path) or _NAMED_GROUP.fullmatch(path)
    if match is None:
        return _refuse(
            RULE_K8S_PATH,
            "The watcher's Kubernetes route lists and watches events, and reads one named object; "
            "this path is neither.",
        )
    if match.group("resource") in REFUSED_PATH_SEGMENTS or match.group("name") in DOT_SEGMENTS:
        return _refuse(
            RULE_K8S_LEGACY,
            "The watcher's Kubernetes route refuses the legacy watch/ and proxy/ paths and dot segments.",
        )
    if match.group("resource") in REFUSED_RESOURCES:
        return _refuse(RULE_K8S_SECRETS, "The watcher's Kubernetes route never reads a Secret.")
    if not set(query) <= NAMED_QUERY_KEYS:
        return _refuse(RULE_K8S_QUERY, "A named read carries a query key this route does not forward.")
    return Decision(allowed=True, rule_id="kubernetes.watcher.named-get", message="")


# Go's strconv.ParseBool true spellings, which the API server reads `watch` with.
WATCH_TRUE = frozenset({"1", "t", "T", "TRUE", "true", "True"})


def is_watch(query: dict[str, Any]) -> bool:
    """True when an events request asks for a watch stream.

    Decided as the API server decides it: the first `watch` value, read with
    ParseBool. Anything the server would stream is therefore under the watch
    caps here.
    """
    values = query.get("watch") or []
    return bool(values) and values[0] in WATCH_TRUE
