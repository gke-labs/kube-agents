"""
Entry 13 of docs/designs/upgrade-failure-catalogue.md: the container runtime changes.

Every supported GKE node runs containerd; what a node pool upgrade can change is the
containerd major the target's node image ships. A pod that talks to the runtime through
its socket, mounted from the host, is what that change breaks: a client pinned to the
`v1alpha2` CRI API, an agent reading the containerd 1.x configuration layout. The rule
pairs the two reads. The runtime side compares each pool's current containerd major,
read from its nodes' `status.nodeInfo.containerRuntimeVersion` (or, when the Nodes were
not read or no node of the pool reports a runtime, from the table by the pool's minor,
saying which) with the major the target's node image
ships, from `upgrade_shape_tables.CONTAINERD_MAJOR_BY_GKE_MINOR` for the pool's image
family. The workload side is every pod or template mounting a runtime socket, or the directory
it sits in (`/run`, `/var/run`, `/run/containerd`, `/var/run/containerd`), through a
`hostPath` volume, GKE's own agents included. A pool whose major changes at the target
with socket clients in the cluster is a risk naming the pool, both majors and the
clients' owners; a change with no client, and clients with no change, are notes; a
change on a member whose Pods or DaemonSets were not read is `unknown`, since the
clients could not be read. `CAN_BLOCK` is False: the change breaks the agent, not the
upgrade, so an `unknown` here is reported in the note and leaves the verdict to the
other rules.

Source for the table: upgrade_shape_tables.py beside this package.
"""

import re

from readiness_rules import (
    CONTEXT_POOLS,
    KIND_CRONJOB,
    KIND_DAEMONSET,
    KIND_DEPLOYMENT,
    KIND_NODE,
    KIND_STATEFULSET,
    LIST_SEPARATOR,
    NODE_READ_KIND,
    POD_READ_KIND,
    active_pods,
    hostpath_volumes,
    items_of_kind,
    new_result,
    node_pool,
    owner_label,
    pod_owner,
    pool_config,
    read_error,
    read_items,
    template_specs,
)
from upgrade_shape_tables import containerd_major_at, node_image_os_family

RULE_ID = "container-runtime"
ENTRY = 13
CAN_BLOCK = False

# The read kind the workload side needs besides the Pods, as `read_errors` keys it; the
# Deployments and StatefulSets come with the PDB read, which `read_items` is empty without.
DAEMONSET_READ_KIND = "daemonset"
SOCKET_CLIENT_KINDS = (KIND_DAEMONSET, KIND_DEPLOYMENT, KIND_STATEFULSET, KIND_CRONJOB)
# The runtime sockets a node agent mounts; `/var/run` is a link to `/run`, so both spellings.
SOCKET_PATHS = ("/var/run/docker.sock", "/run/docker.sock", "/run/containerd/containerd.sock", "/var/run/containerd/containerd.sock")
# The directories a socket sits in, which an agent mounts whole to reach it (Falco mounts
# `/run/containerd`, Datadog `/var/run/containerd`, cAdvisor-style agents `/var/run` or `/run`);
# a wider mount (`/var`, `/`) reaches it too and is not read as a runtime client.
SOCKET_DIRECTORIES = ("/run", "/var/run", "/run/containerd", "/var/run/containerd")
PATH_SEPARATOR = "/"
IMAGE_TYPE_KEY = "imageType"
# `containerd://1.7.34` as a node reports it.
RUNTIME_VERSION_RE = re.compile(r"^(?P<runtime>[a-z-]+)://(?P<major>\d+)\.")
NODE_INFO_KEY = "nodeInfo"
RUNTIME_VERSION_KEY = "containerRuntimeVersion"
MINOR_TEXT = "{major}.{minor}"

CURRENT_OBSERVED = "containerd {major} on its nodes ({version})"
CURRENT_FROM_TABLE_NODES_UNREAD = "containerd {major} by its version {minor}, nodes unread"
CURRENT_FROM_TABLE_NO_NODE = "containerd {major} by its version {minor}; no node of the pool reports a runtime"
FINDING_TEXT = "pool {pool} ({image}) runs {current} and the target's node image ships containerd {target_major}; mounting the runtime socket: {clients}"
CLIENT_TEXT = "{owner} ({paths})"
NOTE_CHANGE_NO_CLIENTS = "pool {pool} ({image}) runs {current} and the target's node image ships containerd {target_major}; no pod mounts the runtime socket"
NOTE_CLIENTS_NO_CHANGE = "{count} workload(s) mount the container runtime socket ({clients}); no pool's containerd major changes at the target"
UNKNOWN_NO_TARGET = "{count} workload(s) mount the container runtime socket; no target, so whether a pool's containerd major changes is unknown"
UNKNOWN_VERSION = "pool {pool}: version unparsable, so whether its containerd major changes at the target is unknown"
UNKNOWN_CLIENTS_UNREAD = "pool {pool} ({image}) runs {current} and the target's node image ships containerd {target_major}; {kind} not read ({error}), so whether a pod mounts the runtime socket is unknown"


def socket_mount(path: str) -> bool:
    """Whether a hostPath reaches the runtime socket: the socket itself, or a directory it sits in."""
    normalized = str(path or "").rstrip(PATH_SEPARATOR) or str(path or "")
    return normalized in SOCKET_PATHS or normalized in SOCKET_DIRECTORIES


def socket_clients(items) -> list[dict]:
    """Pod owners whose spec mounts a runtime socket, or the directory it sits in, through a
    hostPath volume, from the pods and the templates read, each once."""
    clients: dict[str, dict] = {}
    specs = [(pod_owner(pod), pod.get("spec") or {}) for pod in active_pods(items)] + template_specs(items, SOCKET_CLIENT_KINDS)
    for owner, spec in specs:
        paths = sorted({path for path in hostpath_volumes(spec) if socket_mount(path)})
        if not paths:
            continue
        label = owner_label(owner)
        entry = clients.setdefault(label, {"owner": label, "paths": []})
        entry["paths"] = sorted(set(entry["paths"]) | set(paths))
    return [clients[label] for label in sorted(clients)]


def _image_type(pool: dict) -> str:
    return str(pool_config(pool).get(IMAGE_TYPE_KEY) or "").upper()


def observed_runtime(nodes: list[dict], pool: str) -> tuple[str, int] | None:
    """(version string, major) of the lowest containerd major the pool's nodes report."""
    lowest = None
    for node in nodes:
        if node_pool(node) != pool:
            continue
        version = str(((node.get("status") or {}).get(NODE_INFO_KEY) or {}).get(RUNTIME_VERSION_KEY) or "")
        m = RUNTIME_VERSION_RE.match(version)
        if m and (lowest is None or int(m.group("major")) < lowest[1]):
            lowest = (version, int(m.group("major")))
    return lowest


def current_major(pool: dict, pool_version, nodes: list[dict], nodes_unread: bool = False) -> tuple[int, str]:
    """The pool's containerd major now and how it was read: from its nodes, else from the
    table by its minor, saying whether the Nodes were not read or none of the pool's reports
    a runtime (an empty pool, or nodes not yet registered)."""
    observed = observed_runtime(nodes, pool.get("name", ""))
    if observed is not None:
        return observed[1], CURRENT_OBSERVED.format(major=observed[1], version=observed[0])
    minor = (pool_version[0], pool_version[1])
    major = containerd_major_at(minor, node_image_os_family(_image_type(pool)))
    form = CURRENT_FROM_TABLE_NODES_UNREAD if nodes_unread else CURRENT_FROM_TABLE_NO_NODE
    return major, form.format(major=major, minor=MINOR_TEXT.format(major=minor[0], minor=minor[1]))


def evaluate(cluster: dict, member: dict, read, target, context) -> dict:
    out = new_result()
    items = read_items(read)
    clients_unread = {kind: error for kind in (POD_READ_KIND, DAEMONSET_READ_KIND) if (error := read_error(read, kind))}
    clients = socket_clients(items) if not clients_unread else []
    nodes_unread = bool(read_error(read, NODE_READ_KIND))
    nodes = [] if nodes_unread else items_of_kind(items, KIND_NODE)
    pools_parsed = {p.get("name"): p.get("parsed") for p in ((context or {}).get(CONTEXT_POOLS) or []) if isinstance(p, dict)}
    if target is None:
        if clients:
            out["unknown"].append(UNKNOWN_NO_TARGET.format(count=len(clients)))
        return out
    changes = False
    for pool in cluster.get("nodePools") or []:
        if not isinstance(pool, dict):
            continue
        name = pool.get("name", "")
        version = pools_parsed.get(name)
        if version is None:
            if clients:
                out["unknown"].append(UNKNOWN_VERSION.format(pool=name))
            continue
        if version >= target:
            continue
        image = _image_type(pool)
        now, current = current_major(pool, version, nodes, nodes_unread)
        target_major = containerd_major_at((target[0], target[1]), node_image_os_family(image))
        if target_major <= now:
            continue
        changes = True
        if clients_unread:
            kind, error = next(iter(clients_unread.items()))
            out["unknown"].append(UNKNOWN_CLIENTS_UNREAD.format(pool=name, image=image, current=current, target_major=target_major, kind=kind, error=error))
        elif clients:
            out["risks"].append({"pool": name, "image_type": image, "current": current, "current_major": now, "target_major": target_major, "clients": clients})
        else:
            out["notes"].append(NOTE_CHANGE_NO_CLIENTS.format(pool=name, image=image, current=current, target_major=target_major))
    if clients and not changes:
        out["notes"].append(NOTE_CLIENTS_NO_CHANGE.format(count=len(clients), clients=LIST_SEPARATOR.join(c["owner"] for c in clients)))
    return out


def describe(finding: dict) -> str:
    clients = LIST_SEPARATOR.join(CLIENT_TEXT.format(owner=c["owner"], paths=LIST_SEPARATOR.join(c["paths"])) for c in finding["clients"])
    return FINDING_TEXT.format(pool=finding["pool"], image=finding["image_type"], current=finding["current"], target_major=finding["target_major"], clients=clients)
