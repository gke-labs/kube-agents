"""
Entry 21 of docs/designs/upgrade-failure-catalogue.md: the load balancer's connection drain
outlasts the node's deregistration wait.

When a node pool upgrade removes a node that is a backend of a load balancer, GKE
deregisters the node from the backend service and waits for the service's connection
draining, up to DEREGISTRATION_WAIT_SECONDS per node. A backend service whose
`connectionDraining.drainingTimeoutSec` is above that wait outlasts it: GKE stops waiting at
DEREGISTRATION_WAIT_SECONDS, but the instance group's delete of the old VM waits out the full
drain, so each node's removal takes the whole drain and, past about 30 minutes, GKE fails the
node step (`Failed to update node pool`), leaving the pool in ERROR. The rule reads the project's
backend services (the report's `backend_services` read) and files one blocker per backend
service, naming every matched pool and NEG once (a multi-zone pool has one instance group per
zone, and a NEG one per zone), whose backends include one of the cluster's node-pool instance groups
(`nodePools[].instanceGroupUrls`, compared by zone and name, which is how a backend's
`group` URL and a pool's instance-group-manager URL spell one group) or a network endpoint
group the cluster's Services carry in their `cloud.google.com/neg-status` annotation (the
Services the webhook read returned), with a draining timeout above the wait. The fix is one
gcloud command, in the finding.

Unread: the unmanaged `k8s-ig--<hash>` instance groups GKE creates for an instance-group
Service of type LoadBalancer carry the cluster's UID hash, not its name, and a membership
read per group is not made here, so a backend service on one of them is not matched; a
NEG-backed service is matched to its Service rather than a pool, since its endpoints are
pods on any pool.
"""

import json

from readiness_rules import (
    HOLDS_NODE_POOL,
    LIST_SEPARATOR,
    PATH_SEPARATOR,
    PROJECT_READ_BACKEND_SERVICES,
    new_result,
    project_read,
)

RULE_ID = "lb-connection-draining"
ENTRY = 21
CAN_BLOCK = True
HOLDS = (HOLDS_NODE_POOL,)  # the drain holds a node's removal; a control-plane move removes none

# GKE's per-node deregistration wait during a pool upgrade: the longest it waits for a backend
# service's connection draining before stopping its wait (while Compute Engine waits out the full drain).
DEREGISTRATION_WAIT_SECONDS = 600
# Where a backend's group and a pool's manager URL carry the zone and the group name:
# `.../zones/<zone>/instanceGroups/<name>` and `.../zones/<zone>/instanceGroupManagers/<name>`;
# a NEG is `.../zones/<zone>/networkEndpointGroups/<name>`.
ZONES_SEGMENT = "zones"
REGIONS_SEGMENT = "regions"
INSTANCE_GROUPS_SEGMENT = "instanceGroups"
INSTANCE_GROUP_MANAGERS_SEGMENT = "instanceGroupManagers"
NEG_SEGMENT = "networkEndpointGroups"
URL_TAIL_PARTS = 4  # <scope value>/<kind>/<name>, and the segment before the scope that names its kind
# The cluster record's and the backend service's fields.
NODE_POOLS_KEY = "nodePools"
POOL_NAME_KEY = "name"
INSTANCE_GROUP_URLS_KEY = "instanceGroupUrls"
BACKENDS_KEY = "backends"
GROUP_KEY = "group"
CONNECTION_DRAINING_KEY = "connectionDraining"
DRAINING_TIMEOUT_KEY = "drainingTimeoutSec"
BACKEND_SERVICE_NAME_KEY = "name"
BACKEND_SERVICE_REGION_KEY = "region"
# The NEG controller's annotation on a Service: `{"network_endpoint_groups": {"80": "k8s1-..."}, "zones": [...]}`.
KIND_SERVICE = "Service"
NEG_STATUS_ANNOTATION = "cloud.google.com/neg-status"
NEG_STATUS_GROUPS_KEY = "network_endpoint_groups"
SERVICE_LABEL_FORMAT = "{namespace}/{name}"
# The fix, as the finding spells it: the backend service's scope flag and the wait.
GLOBAL_SCOPE_FLAG = "--global"
REGION_SCOPE_FLAG_FORMAT = "--region {region}"
ACTION_FORMAT = "gcloud compute backend-services update {name} {scope} --connection-draining-timeout={wait}"
FINDING_FORMAT = (
    "BackendService {name} drains for {timeout} s on {targets}; GKE waits {wait} s per node during an upgrade, "
    "so each surge node's drain times out; set the timeout at or under the wait: `{action}`"
)
# What one service's finding names: its matched pools once each, its matched NEGs once each.
POOLS_TARGET_FORMAT = "pool(s) {pools}"
NEG_TARGET_FORMAT = "NEG {group} of Service {service}"
TARGETS_JOIN = " and "
# Both counts are backend services: the ones with a backend on this cluster, of the project's.
CLEAN_NOTE_FORMAT = "no backend service on this cluster's pools or NEGs drains for more than {wait} s ({matched} matched of {total} in the project)"
SERVICES_NOT_READ_REASON = "the Services were not read ({error}), so a NEG-backed backend service was not matched"
BACKEND_SERVICES_NOT_A_LIST = "the backend-services read returned {type}, not a list"


def _url_parts(url: str) -> tuple[str, str, str]:
    """(scope segment's value, kind segment, name) of a compute URL: `.../zones/us-central1-a/instanceGroups/x`
    -> (`us-central1-a`, `instanceGroups`, `x`); empty strings when the URL has no such tail."""
    parts = str(url or "").rstrip(PATH_SEPARATOR).split(PATH_SEPARATOR)
    if len(parts) < URL_TAIL_PARTS:
        return "", "", parts[-1] if parts else ""
    return parts[-3], parts[-2], parts[-1]


def pool_groups(cluster: dict) -> dict[tuple[str, str], str]:
    """{(zone, group name): pool name} for every instance group the cluster's pools manage."""
    out = {}
    for pool in cluster.get(NODE_POOLS_KEY) or []:
        if not isinstance(pool, dict):
            continue
        for url in pool.get(INSTANCE_GROUP_URLS_KEY) or []:
            zone, _, name = _url_parts(url)
            out[(zone, name)] = str(pool.get(POOL_NAME_KEY) or "")
    return out


def service_negs(services) -> dict[str, str]:
    """{NEG name: `namespace/name` of the Service} from the NEG controller's status annotations."""
    out = {}
    for service in services or []:
        if not isinstance(service, dict) or service.get("kind") != KIND_SERVICE:
            continue
        meta = service.get("metadata") or {}
        raw = (meta.get("annotations") or {}).get(NEG_STATUS_ANNOTATION)
        if not raw:
            continue
        try:
            status = json.loads(raw)
        except ValueError:
            continue
        groups = status.get(NEG_STATUS_GROUPS_KEY) if isinstance(status, dict) else None
        for neg in (groups or {}).values():
            out[str(neg)] = SERVICE_LABEL_FORMAT.format(namespace=meta.get("namespace", ""), name=meta.get("name", ""))
    return out


def _scope(backend_service: dict) -> str:
    region = backend_service.get(BACKEND_SERVICE_REGION_KEY)
    if not region:
        return GLOBAL_SCOPE_FLAG
    return REGION_SCOPE_FLAG_FORMAT.format(region=str(region).rstrip(PATH_SEPARATOR).rsplit(PATH_SEPARATOR, 1)[-1])


def _timeout(backend_service: dict) -> int:
    draining = backend_service.get(CONNECTION_DRAINING_KEY)
    value = draining.get(DRAINING_TIMEOUT_KEY) if isinstance(draining, dict) else None
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def evaluate(cluster: dict, member: dict, read, target, context) -> dict:
    out = new_result()
    backend_services, error = project_read(context, PROJECT_READ_BACKEND_SERVICES)
    if error is not None:
        out["unknown"].append(error)
        return out
    if not isinstance(backend_services, list):
        out["unknown"].append(BACKEND_SERVICES_NOT_A_LIST.format(type=type(backend_services).__name__))
        return out
    groups = pool_groups(cluster)
    services = (read or {}).get("webhook_items")
    negs = service_negs(services) if services is not None else {}
    matched = 0
    saw_neg_backend = False
    for backend_service in backend_services:
        if not isinstance(backend_service, dict):
            continue
        timeout = _timeout(backend_service)
        pools: list[str] = []
        neg_matches: list[tuple[str, str]] = []
        for backend in backend_service.get(BACKENDS_KEY) or []:
            if not isinstance(backend, dict):
                continue
            zone, kind, name = _url_parts(backend.get(GROUP_KEY, ""))
            if kind in (INSTANCE_GROUPS_SEGMENT, INSTANCE_GROUP_MANAGERS_SEGMENT):
                pool = groups.get((zone, name))
                if pool is not None and pool not in pools:
                    pools.append(pool)
            elif kind == NEG_SEGMENT:
                saw_neg_backend = True
                service = negs.get(name)
                if service is not None and (name, service) not in neg_matches:
                    neg_matches.append((name, service))
        if not pools and not neg_matches:
            continue
        matched += 1  # one service, however many of its backends (one per zone) are the cluster's
        if timeout <= DEREGISTRATION_WAIT_SECONDS:
            continue
        bs_name = str(backend_service.get(BACKEND_SERVICE_NAME_KEY) or "")
        scope = _scope(backend_service)
        out["blocking"].append({
            "backend_service": bs_name,
            "scope": scope,
            "timeout_seconds": timeout,
            "wait_seconds": DEREGISTRATION_WAIT_SECONDS,
            "pools": pools,
            "negs": [{"group": group, "service": service} for group, service in neg_matches],
            "action": ACTION_FORMAT.format(name=bs_name, scope=scope, wait=DEREGISTRATION_WAIT_SECONDS),
        })
    if services is None and saw_neg_backend:
        out["unknown"].append(SERVICES_NOT_READ_REASON.format(error=(read or {}).get("webhook_error") or "no Service list"))
    if not out["blocking"]:
        out["notes"].append(CLEAN_NOTE_FORMAT.format(wait=DEREGISTRATION_WAIT_SECONDS, matched=matched, total=len(backend_services)))
    return out


def _targets_text(finding: dict) -> str:
    parts = []
    if finding.get("pools"):
        parts.append(POOLS_TARGET_FORMAT.format(pools=LIST_SEPARATOR.join(finding["pools"])))
    if finding.get("negs"):
        parts.append(LIST_SEPARATOR.join(NEG_TARGET_FORMAT.format(group=neg["group"], service=neg["service"]) for neg in finding["negs"]))
    return TARGETS_JOIN.join(parts)


def describe(finding: dict) -> str:
    return FINDING_FORMAT.format(name=finding["backend_service"], timeout=finding["timeout_seconds"], targets=_targets_text(finding), wait=finding["wait_seconds"], action=finding["action"])
