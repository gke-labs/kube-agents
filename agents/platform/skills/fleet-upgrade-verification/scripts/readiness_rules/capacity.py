"""
Entry 2 of docs/designs/upgrade-failure-catalogue.md: no spare capacity for the
displaced pods.

On GKE's default surge settings (maxSurge 1, maxUnavailable 0) the replacement node
exists before a node is drained, so the entry needs a pool that removes a node first:
`maxUnavailable` above 0 on the surge strategy, or a blue-green upgrade with the
autoscaled rollout policy, whose green pool starts empty. On such a pool the pods of
the node being drained need a node to land on. The rule measures that by placing them:
for each node of the pool in turn, taken as the one drained, it places the node's pods,
largest first, onto the schedulable nodes left in the cluster that each pod may schedule
on (its node selector, its required node affinity and the taints it tolerates decide
which; a tainted accelerator pool's spare capacity never counts for a pod without the
toleration; a cordoned node is out twice over, through the `spec.unschedulable` the
projected node read carries and through the `unschedulable` taint a cordon adds), each
onto the eligible node with the most room left, by CPU, memory and extended-resource
requests (`nvidia.com/gpu`, `google.com/tpu`: a pod holding one lands only on a node with
one free) against allocatable minus what runs there. A pod no eligible node can take is
stranded. DaemonSet and mirror pods are left out: the kubelet recreates them on the
rebuilt node, so a drain never has to find them room. The node measured is the one
whose stranded pods are the largest in either resource, so a memory-bound node is
measured as well as a CPU-bound one; the finding names every other node of the pool
that strands pods too.

Blocking: maxUnavailable above 0 with maxSurge 0 on a pool the autoscaler cannot grow
(at its ceiling, or not autoscaled) that strands a pod. Risk: a stranded pod on a pool
that keeps some surge or can still grow, or on an autoscaled blue-green pool. A pool
that removes a node first and places every node's pods is a note, so the setting stays
visible. Placement is a heuristic (largest pod first, most room first), so a finding
says a drain strands these pods under that placement, and a note says one placement
exists. The rule reads the Nodes and the Pods; a pool that removes a node first is
`unknown` when either was not read. Autopilot members are not graded: GKE owns their
pools and their surge settings.

The same pool list is read for the other side of the entry, the surge node that must
exist before a drain: a pool on the surge strategy with `maxSurge` above 0 (GKE's default
is 1) creates that many nodes beyond its size while it upgrades. Each surge node needs a
Pod CIDR block of the pool's per-node size (the prefix its nodes' `spec.podCIDR` carries,
else the block GKE sizes for the pool's pod limit) in the pool's Pod range
(`networkConfig.podIpv4CidrBlock`, else the cluster's `clusterIpv4Cidr`). The free aligned
blocks of that range, those no node's block touches, against the surge nodes the pools
below the target need (the sum of their `maxSurge`, counted per pool, not per zone; a
blue-green pool creates its green pool instead and is not counted) is a blocker when the
free blocks are fewer: a node without a block never registers, and the pool upgrade stalls
between versions. Where the pools' blocks differ in size the largest is the unit, so the
count is the conservative one. And a pool carrying accelerators (`config.accelerators`)
that surges with no reservation affinity (`config.reservationAffinity` consuming neither
ANY_RESERVATION nor SPECIFIC_RESERVATION) is a blocker: a surge node needs accelerator
capacity the zone may not have, the shape in which a GPU pool upgrade stalled halfway on
capacity and left the pool on two versions; one under a reservation is a note. The surge
halves need the target (which pools upgrade) and the Nodes (the blocks held): without the
target they are one `unknown`, and the Pod range half is `unknown` when the Nodes were not
read or the record carries no Pod range. Quota is not read here: the stockout-prevention
audit reads it.

Source for the settings and the strategies:
https://docs.cloud.google.com/kubernetes-engine/docs/concepts/node-pool-upgrade-strategies
Source for the per-node block GKE sizes from the pod limit:
https://docs.cloud.google.com/kubernetes-engine/docs/how-to/flexible-pod-cidr
"""

import ipaddress
import math

from readiness_rules import (
    CONTEXT_AUTOPILOT,
    CONTEXT_POOLS,
    KIND_NODE,
    LIST_SEPARATOR,
    NODE_READ_KIND,
    POD_READ_KIND,
    active_pods,
    format_cpu,
    format_memory,
    is_daemonset_pod,
    is_mirror_pod,
    items_of_kind,
    new_result,
    node_allocatable,
    node_extended_allocatable,
    node_pool,
    node_schedulable,
    owner_label,
    pod_extended_requests,
    pod_owner,
    pod_requests,
    read_error,
    read_items,
)

RULE_ID = "surge-capacity"
ENTRY = 2
CAN_BLOCK = True

# Upgrade settings as `nodePools[].upgradeSettings` spells them. A pool with no block is
# on GKE's defaults; inside a block the API omits a zero `maxSurge` (the fixture catalogue's
# state assertion for `readiness-surge-blocked` reads the same), so an absent key is 0.
UPGRADE_SETTINGS_KEY = "upgradeSettings"
STRATEGY_KEY = "strategy"
STRATEGY_SURGE = "SURGE"
STRATEGY_BLUE_GREEN = "BLUE_GREEN"
MAX_SURGE_KEY = "maxSurge"
MAX_UNAVAILABLE_KEY = "maxUnavailable"
DEFAULT_MAX_SURGE = 1
DEFAULT_MAX_UNAVAILABLE = 0
BLUE_GREEN_SETTINGS_KEY = "blueGreenSettings"
AUTOSCALED_ROLLOUT_KEY = "autoscaledRolloutPolicy"
# Autoscaler limits as `nodePools[].autoscaling` spells them: a total across zones when
# set, else a per-zone maximum times the pool's zones.
AUTOSCALING_KEY = "autoscaling"
AUTOSCALING_ENABLED_KEY = "enabled"
TOTAL_MAX_NODES_KEY = "totalMaxNodeCount"
MAX_NODES_PER_ZONE_KEY = "maxNodeCount"
POOL_LOCATIONS_KEY = "locations"
CLUSTER_LOCATIONS_KEY = "locations"
SINGLE_ZONE = 1
AUTOPILOT_KEY = "autopilot"
AUTOPILOT_ENABLED_KEY = "enabled"
# Scheduling vocabulary, as the pod and node APIs spell it. Required affinity terms are
# read whole (terms are ORed, the expressions in a term ANDed); `matchFields` and an
# operator not listed read as not fitting, so such a pod counts as stranded rather than
# as placed somewhere it may not go.
NODE_SELECTOR_KEY = "nodeSelector"
AFFINITY_KEY = "affinity"
NODE_AFFINITY_KEY = "nodeAffinity"
REQUIRED_AFFINITY_KEY = "requiredDuringSchedulingIgnoredDuringExecution"
NODE_SELECTOR_TERMS_KEY = "nodeSelectorTerms"
MATCH_EXPRESSIONS_KEY = "matchExpressions"
MATCH_FIELDS_KEY = "matchFields"
OP_IN = "In"
OP_NOT_IN = "NotIn"
OP_EXISTS = "Exists"
OP_DOES_NOT_EXIST = "DoesNotExist"
TOLERATION_EQUAL = "Equal"
REPELLING_TAINT_EFFECTS = ("NoSchedule", "NoExecute")
# How many of the largest stranded pods a finding names.
NAMED_WORKLOADS = 3
NODE_COUNT_UNREAD = "?"

# Finding and note text.
WHY_SURGE = "maxSurge {max_surge}, maxUnavailable {max_unavailable}"
WHY_AUTOSCALED_BLUE_GREEN = "autoscaled blue-green, whose green pool starts empty"
SCALING_NOT_AUTOSCALED = "not autoscaled"
SCALING_AT_CEILING = "autoscaler at its ceiling of {ceiling}"
SCALING_CAN_GROW = "autoscaler can grow to {ceiling}"
POOL_SUMMARY = "pool {pool} ({why}; {node_count} node(s), {scaling})"
FINDING_TEXT = (
    "{summary} removes a node before its replacement exists; draining its node {node} leaves "
    "{cpu} CPU / {memory} of pods with no node to take them ({room_cpu} / {room_memory} free on the nodes they may schedule on): {workloads}{also}"
)
WORKLOAD_TEXT = "{owner} ({cpu} / {memory}{extended}{pinned})"
EXTENDED_TEXT = ", {amount:g} {resource}"
WORKLOAD_PINNED_SUFFIX = ", pinned to the pool"
MORE_WORKLOADS = " and {count} more"
ALSO_SHORT_TEXT = "; node(s) {nodes} strand pods as well"
NOTE_HEADROOM = "{summary} removes a node before its replacement exists; the nodes its pods may schedule on have room for every node's pods"
NOTE_EMPTY = "{summary} removes a node before its replacement exists and has no node"
UNKNOWN_NO_TARGET = "{summary}: no target, so whether a node upgrade is pending is unknown"
UNKNOWN_VERSION = "{summary}: version unparsable, so whether a node upgrade is pending is unknown"
UNKNOWN_KIND_UNREAD = "{summary}: {kind} not read ({error}), so its headroom was not measured"
UNKNOWN_QUANTITY = "{summary}: {what} did not parse, so its headroom was not measured"
QUANTITY_POD = "the requests of pod {namespace}/{name}"
QUANTITY_NODE = "the allocatable of node {name}"

# The surge halves. A pool's Pod range is its own secondary range when it has one, else the
# cluster's, under either spelling of the record; each node's block is its `spec.podCIDR`,
# which the report's projected node read carries. GKE sizes a node's block at twice its pod
# limit rounded up to a power of two (110 pods is a /24, 32 pods a /26), the fallback for a
# pool with no node yet, from the pool's limit, else the cluster's default, else a /24.
POOL_NETWORK_CONFIG_KEY = "networkConfig"
POOL_POD_RANGE_KEY = "podIpv4CidrBlock"
CLUSTER_POD_RANGE_KEY = "clusterIpv4Cidr"
IP_ALLOCATION_POLICY_KEY = "ipAllocationPolicy"
IP_ALLOCATION_POD_RANGE_KEY = "clusterIpv4CidrBlock"
MAX_PODS_CONSTRAINT_KEY = "maxPodsConstraint"
CLUSTER_DEFAULT_MAX_PODS_KEY = "defaultMaxPodsConstraint"
MAX_PODS_PER_NODE_KEY = "maxPodsPerNode"
POD_CIDR_KEY = "podCIDR"
POD_BLOCK_ADDRESSES_PER_POD = 2
DEFAULT_POD_BLOCK_PREFIX = 24
IPV4_BITS = 32
IPV4_VERSION = 4
# Accelerators and the reservation a pool consumes, as `nodePools[].config` spells them; a
# pool with no `reservationAffinity`, or one that consumes NO_RESERVATION, holds none.
ACCELERATORS_KEY = "accelerators"
ACCELERATOR_TYPE_KEY = "acceleratorType"
ACCELERATOR_COUNT_KEY = "acceleratorCount"
DEFAULT_ACCELERATOR_COUNT = 1
RESERVATION_AFFINITY_KEY = "reservationAffinity"
RESERVATION_TYPE_KEY = "consumeReservationType"
RESERVATIONS_HELD = ("ANY_RESERVATION", "SPECIFIC_RESERVATION")
# What a finding of this rule is about: the placement half above, or one of the surge halves.
FINDING_KEY = "finding"
FINDING_PLACEMENT = "placement"
FINDING_POD_RANGE = "pod-range"
FINDING_ACCELERATOR = "accelerator"
ACCELERATOR_TEXT = "{count} x {type}"
SURGE_POOL_TEXT = "{pool} (maxSurge {max_surge})"
POD_RANGE_FINDING_TEXT = (
    "pod range {range} has {free} free /{prefix} block(s) ({used} of {total} hold a node) and the {need} surge node(s) of pool(s) {pools} "
    "each need one: a surge node with no block never registers and the pool upgrade stalls between versions; lower max-surge or add a secondary Pod range"
)
ACCELERATOR_FINDING_TEXT = (
    "pool {pool} ({accelerators}) surges (maxSurge {max_surge}) with no reservation affinity: each surge node needs accelerator capacity the zone may not have, "
    "and a pool upgrade that cannot create one stalls between versions; hold a reservation for the surge node, or, on a pool with a spare accelerator node for the drained pods, "
    "set the in-place strategy (max-surge 0, max-unavailable 1)"
)
NOTE_ACCELERATOR_RESERVED = "pool {pool} ({accelerators}) surges (maxSurge {max_surge}) under reservation affinity {reservation}"
UNKNOWN_SURGE_NO_TARGET = "surge pool(s) {pools}: no target, so whether their surge nodes are needed in this upgrade is unknown"
UNKNOWN_SURGE_VERSION = "surge pool {pool}: version unparsable, so whether its surge nodes are needed in this upgrade is unknown"
UNKNOWN_POD_RANGE_UNREAD = "pod range headroom for the surge node(s) of pool(s) {pools}: {kind} not read ({error}), so the free blocks were not counted"
UNKNOWN_POD_RANGE_MISSING = "pod range headroom for the surge node(s) of pool(s) {pools}: the cluster record carries no Pod range, so the free blocks were not counted"
UNKNOWN_POD_RANGE_UNPARSABLE = "pod range headroom for the surge node(s) of pool(s) {pools}: the Pod range {range!r} did not parse, so the free blocks were not counted"


def _int(value, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def pool_settings(pool: dict) -> dict:
    """strategy, maxSurge, maxUnavailable and whether the blue-green rollout is autoscaled."""
    settings = pool.get(UPGRADE_SETTINGS_KEY)
    if not isinstance(settings, dict):
        return {"strategy": STRATEGY_SURGE, "max_surge": DEFAULT_MAX_SURGE, "max_unavailable": DEFAULT_MAX_UNAVAILABLE, "autoscaled_blue_green": False}
    strategy = settings.get(STRATEGY_KEY) or STRATEGY_SURGE
    blue_green = settings.get(BLUE_GREEN_SETTINGS_KEY) or {}
    return {
        "strategy": strategy,
        "max_surge": _int(settings.get(MAX_SURGE_KEY), 0),
        "max_unavailable": _int(settings.get(MAX_UNAVAILABLE_KEY), 0),
        "autoscaled_blue_green": strategy == STRATEGY_BLUE_GREEN and isinstance(blue_green, dict) and AUTOSCALED_ROLLOUT_KEY in blue_green,
    }


def removes_node_first(settings: dict) -> bool:
    """Whether the pool's upgrade takes a node away before its replacement exists."""
    return (settings["strategy"] == STRATEGY_SURGE and settings["max_unavailable"] > 0) or settings["autoscaled_blue_green"]


def autoscaler_ceiling(pool: dict, cluster: dict) -> tuple[bool, int | None]:
    """(autoscaled, the most nodes the autoscaler may give the pool)."""
    autoscaling = pool.get(AUTOSCALING_KEY) or {}
    if not isinstance(autoscaling, dict) or not autoscaling.get(AUTOSCALING_ENABLED_KEY):
        return False, None
    total = _int(autoscaling.get(TOTAL_MAX_NODES_KEY), 0)
    if total > 0:
        return True, total
    zones = len(pool.get(POOL_LOCATIONS_KEY) or cluster.get(CLUSTER_LOCATIONS_KEY) or []) or SINGLE_ZONE
    return True, _int(autoscaling.get(MAX_NODES_PER_ZONE_KEY), 0) * zones


def surges(settings: dict) -> bool:
    """Whether the pool's upgrade creates surge nodes: the surge strategy with maxSurge above 0."""
    return settings["strategy"] == STRATEGY_SURGE and settings["max_surge"] > 0


def _config(pool: dict) -> dict:
    config = pool.get("config")
    return config if isinstance(config, dict) else {}


def pool_accelerators(pool: dict) -> list[dict]:
    """The accelerators the pool's nodes carry, each with a type."""
    return [a for a in _config(pool).get(ACCELERATORS_KEY) or [] if isinstance(a, dict) and a.get(ACCELERATOR_TYPE_KEY)]


def accelerators_text(accelerators: list[dict]) -> str:
    return LIST_SEPARATOR.join(ACCELERATOR_TEXT.format(count=_int(a.get(ACCELERATOR_COUNT_KEY), DEFAULT_ACCELERATOR_COUNT), type=a.get(ACCELERATOR_TYPE_KEY)) for a in accelerators)


def pool_reservation(pool: dict) -> str | None:
    """The reservation type the pool's nodes consume, or None when they hold none."""
    affinity = _config(pool).get(RESERVATION_AFFINITY_KEY)
    kind = affinity.get(RESERVATION_TYPE_KEY) if isinstance(affinity, dict) else None
    return kind if kind in RESERVATIONS_HELD else None


def pool_pod_range(pool: dict, cluster: dict) -> str | None:
    """The Pod range the pool's nodes draw their blocks from: its own, else the cluster's."""
    network = pool.get(POOL_NETWORK_CONFIG_KEY)
    own = network.get(POOL_POD_RANGE_KEY) if isinstance(network, dict) else None
    policy = cluster.get(IP_ALLOCATION_POLICY_KEY)
    shared = cluster.get(CLUSTER_POD_RANGE_KEY) or (policy.get(IP_ALLOCATION_POD_RANGE_KEY) if isinstance(policy, dict) else None)
    return own or shared or None


def pod_network(text):
    """The IPv4 network a Pod range or a node's block names, or None."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        network = ipaddress.ip_network(text.strip(), strict=False)
    except ValueError:
        return None
    return network if network.version == IPV4_VERSION else None


def block_prefix_for_pods(max_pods) -> int | None:
    """The prefix of the block GKE sizes for a pod limit: twice the limit, rounded up to a power of two."""
    pods = _int(max_pods, 0)
    if pods <= 0:
        return None
    return IPV4_BITS - math.ceil(math.log2(pods * POD_BLOCK_ADDRESSES_PER_POD))


def pool_block_prefix(pool: dict, cluster: dict, pool_nodes: list[dict]) -> int:
    """The per-node block prefix of the pool: the largest block among its nodes' `spec.podCIDR`,
    else the block GKE sizes for its pod limit (the pool's, else the cluster's default), else a /24."""
    prefixes = [network.prefixlen for network in (pod_network((n.get("spec") or {}).get(POD_CIDR_KEY)) for n in pool_nodes) if network is not None]
    if prefixes:
        return min(prefixes)
    for holder, key in ((pool, MAX_PODS_CONSTRAINT_KEY), (cluster, CLUSTER_DEFAULT_MAX_PODS_KEY)):
        constraint = holder.get(key)
        prefix = block_prefix_for_pods(constraint.get(MAX_PODS_PER_NODE_KEY)) if isinstance(constraint, dict) else None
        if prefix is not None:
            return prefix
    return DEFAULT_POD_BLOCK_PREFIX


def free_blocks(pod_range, prefix: int, nodes: list[dict]) -> tuple[int, int, int]:
    """(free, held, total): the aligned /`prefix` blocks of `pod_range` no node's block touches,
    the blocks the nodes hold, and the range's count of them. A node block larger than the unit
    holds every unit in it, a smaller one the unit it sits in, and a node outside the range
    holds none of it; a unit larger than the range is the range itself."""
    unit = max(prefix, pod_range.prefixlen)
    total = 1 << (unit - pod_range.prefixlen)
    shift = IPV4_BITS - unit
    held: set[int] = set()
    for node in nodes:
        network = pod_network((node.get("spec") or {}).get(POD_CIDR_KEY))
        if network is None or not network.subnet_of(pod_range):
            continue
        first = (int(network.network_address) - int(pod_range.network_address)) >> shift
        last = (int(network.broadcast_address) - int(pod_range.network_address)) >> shift
        held.update(range(first, last + 1))
    return total - len(held), len(held), total


def _term_matches(term: dict, labels: dict) -> bool:
    if term.get(MATCH_FIELDS_KEY):
        return False
    for req in term.get(MATCH_EXPRESSIONS_KEY) or []:
        if not isinstance(req, dict):
            return False
        key, op, values = req.get("key"), req.get("operator"), req.get("values") or []
        if op == OP_IN:
            if labels.get(key) not in values:
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


def _tolerates(tolerations: list, taint: dict) -> bool:
    for toleration in tolerations:
        if not isinstance(toleration, dict):
            continue
        key = toleration.get("key")
        op = toleration.get("operator") or TOLERATION_EQUAL
        effect = toleration.get("effect")
        if key and key != taint.get("key"):
            continue
        if not key and op != OP_EXISTS:
            continue
        if op != OP_EXISTS and toleration.get("value") != taint.get("value"):
            continue
        if not effect or effect == taint.get("effect"):
            return True
    return False


def _required_terms(spec: dict) -> list:
    node_affinity = (spec.get(AFFINITY_KEY) or {}).get(NODE_AFFINITY_KEY) or {}
    required = node_affinity.get(REQUIRED_AFFINITY_KEY) or {}
    return required.get(NODE_SELECTOR_TERMS_KEY) or [] if isinstance(required, dict) else []


def node_fits_pod(node: dict, spec: dict) -> bool:
    """Whether the pod's selector, required affinity and tolerations admit this node.
    Resources are not compared here; the placement does that."""
    labels = (node.get("metadata") or {}).get("labels") or {}
    for key, value in (spec.get(NODE_SELECTOR_KEY) or {}).items():
        if labels.get(key) != value:
            return False
    terms = _required_terms(spec)
    if terms and not any(isinstance(term, dict) and _term_matches(term, labels) for term in terms):
        return False
    tolerations = spec.get("tolerations") or []
    for taint in (node.get("spec") or {}).get("taints") or []:
        if isinstance(taint, dict) and taint.get("effect") in REPELLING_TAINT_EFFECTS and not _tolerates(tolerations, taint):
            return False
    return True


def _node_name(node: dict) -> str:
    return (node.get("metadata") or {}).get("name", "")


def _free_capacity(nodes: list[dict], pods_by_node: dict) -> dict[str, tuple] | str:
    """Per node, (CPU, memory, {extended resource: amount}): allocatable minus every active
    pod's requests, floored at 0; a string names the quantity that did not parse."""
    free: dict[str, tuple] = {}
    for node in nodes:
        cpu, memory = node_allocatable(node)
        extended = node_extended_allocatable(node)
        if cpu is None or memory is None or extended is None:
            return QUANTITY_NODE.format(name=_node_name(node))
        for pod in pods_by_node.get(_node_name(node), []):
            spec = pod.get("spec") or {}
            pod_cpu, pod_memory = pod_requests(spec)
            pod_extended = pod_extended_requests(spec)
            if pod_cpu is None or pod_memory is None or pod_extended is None:
                meta = pod.get("metadata") or {}
                return QUANTITY_POD.format(namespace=meta.get("namespace", ""), name=meta.get("name", ""))
            cpu -= pod_cpu
            memory -= pod_memory
            extended = {key: extended.get(key, 0) - pod_extended.get(key, 0) if key in pod_extended else extended[key] for key in set(extended) | set(pod_extended)}
        free[_node_name(node)] = (max(cpu, 0), max(memory, 0), {key: max(amount, 0) for key, amount in extended.items()})
    return free


def _displaced(node: dict, pods_by_node: dict) -> list[dict] | str:
    """The pods a drain of `node` has to find room for, with their requests."""
    displaced = []
    for pod in pods_by_node.get(_node_name(node), []):
        if is_daemonset_pod(pod) or is_mirror_pod(pod):
            continue
        spec = pod.get("spec") or {}
        cpu, memory = pod_requests(spec)
        extended = pod_extended_requests(spec)
        if cpu is None or memory is None or extended is None:
            meta = pod.get("metadata") or {}
            return QUANTITY_POD.format(namespace=meta.get("namespace", ""), name=meta.get("name", ""))
        displaced.append({"owner": owner_label(pod_owner(pod)), "cpu_m": cpu, "memory_bytes": memory, "extended": extended, "spec": spec})
    return displaced


def place(displaced: list[dict], candidates: list[dict], free: dict[str, tuple], pool_name: str) -> list[dict]:
    """First-fit placement of the displaced pods, largest first, each onto the eligible
    candidate with the most room left by CPU and memory that also has its extended resources
    free (a GPU pod lands only on a node with a GPU to spare); returns the pods no node could take.

    A stranded pod carries `pinned` (no schedulable node outside the pool admits it,
    whatever the room) and `room`, the free capacity the nodes it may schedule on had
    before any placement.
    """
    remaining = dict(free)
    stranded = []
    for pod in sorted(displaced, key=lambda d: (-d["cpu_m"], -d["memory_bytes"], d["owner"])):
        eligible = [n for n in candidates if node_fits_pod(n, pod["spec"])]
        placed = False
        for node in sorted(eligible, key=lambda n: remaining[_node_name(n)][:2], reverse=True):
            cpu, memory, extended = remaining[_node_name(node)]
            if cpu >= pod["cpu_m"] and memory >= pod["memory_bytes"] and all(extended.get(key, 0) >= amount for key, amount in pod["extended"].items()):
                remaining[_node_name(node)] = (cpu - pod["cpu_m"], memory - pod["memory_bytes"], {key: extended.get(key, 0) - pod["extended"].get(key, 0) for key in set(extended) | set(pod["extended"])})
                placed = True
                break
        if placed:
            continue
        stranded.append(
            {
                "owner": pod["owner"],
                "cpu_m": pod["cpu_m"],
                "memory_bytes": pod["memory_bytes"],
                "extended": pod["extended"],
                "pinned": not any(node_pool(n) != pool_name for n in eligible),
                "eligible_nodes": [_node_name(n) for n in eligible],
                "room": {"cpu_m": sum(free[_node_name(n)][0] for n in eligible), "memory_bytes": sum(free[_node_name(n)][1] for n in eligible)},
            }
        )
    return stranded


def measure(pool_name: str, pool_nodes: list[dict], nodes: list[dict], pods: list[dict]) -> dict | str:
    """Every node of the pool drained in turn; the one whose stranded pods are the largest
    in either resource is the measurement. A string names a quantity that did not parse."""
    pods_by_node: dict[str, list[dict]] = {}
    for pod in pods:
        pods_by_node.setdefault((pod.get("spec") or {}).get("nodeName", ""), []).append(pod)
    schedulable = [n for n in nodes if node_schedulable(n)]
    free = _free_capacity(schedulable, pods_by_node)
    if isinstance(free, str):
        return free

    results = []
    for node in pool_nodes:
        displaced = _displaced(node, pods_by_node)
        if isinstance(displaced, str):
            return displaced
        candidates = [n for n in schedulable if _node_name(n) != _node_name(node)]
        stranded = place(displaced, candidates, free, pool_name)
        results.append(
            {
                "node": _node_name(node),
                "displaced": {"cpu_m": sum(d["cpu_m"] for d in displaced), "memory_bytes": sum(d["memory_bytes"] for d in displaced)},
                "stranded": {"cpu_m": sum(s["cpu_m"] for s in stranded), "memory_bytes": sum(s["memory_bytes"] for s in stranded)},
                "stranded_pods": stranded,
            }
        )
    short = [r for r in results if r["stranded_pods"]]
    if not short:
        return {"short": False, "nodes_measured": [r["node"] for r in results]}
    # The worst node in any resource: its stranded requests relative to the largest
    # stranded requests in that resource across the pool's nodes, the larger ratio deciding.
    worst_cpu = max(r["stranded"]["cpu_m"] for r in short) or 1
    worst_memory = max(r["stranded"]["memory_bytes"] for r in short) or 1

    def severity(r: dict) -> tuple:
        return (max(r["stranded"]["cpu_m"] / worst_cpu, r["stranded"]["memory_bytes"] / worst_memory), len(r["stranded_pods"]), r["node"])

    chosen = max(short, key=severity)
    eligible = {n for s in chosen["stranded_pods"] for n in s["eligible_nodes"]}
    room = {"cpu_m": sum(free[n][0] for n in eligible), "memory_bytes": sum(free[n][1] for n in eligible)}
    return {
        "short": True,
        "node": chosen["node"],
        "displaced": chosen["displaced"],
        "stranded": chosen["stranded"],
        "room": room,
        "stranded_count": len(chosen["stranded_pods"]),
        "workloads": [
            {key: s[key] for key in ("owner", "cpu_m", "memory_bytes", "extended", "pinned")}
            for s in sorted(chosen["stranded_pods"], key=lambda s: (-s["cpu_m"], -s["memory_bytes"], s["owner"]))[:NAMED_WORKLOADS]
        ],
        "other_short_nodes": [r["node"] for r in short if r["node"] != chosen["node"]],
    }


def _summary(name: str, settings: dict, node_count, autoscaled: bool, ceiling: int | None, at_ceiling: bool) -> str:
    why = WHY_AUTOSCALED_BLUE_GREEN if settings["autoscaled_blue_green"] else WHY_SURGE.format(max_surge=settings["max_surge"], max_unavailable=settings["max_unavailable"])
    if not autoscaled:
        scaling = SCALING_NOT_AUTOSCALED
    elif at_ceiling:
        scaling = SCALING_AT_CEILING.format(ceiling=ceiling)
    else:
        scaling = SCALING_CAN_GROW.format(ceiling=ceiling)
    return POOL_SUMMARY.format(pool=name, why=why, node_count=node_count, scaling=scaling)


def _is_autopilot(cluster: dict, context) -> bool:
    if isinstance(context, dict) and CONTEXT_AUTOPILOT in context:
        return bool(context[CONTEXT_AUTOPILOT])
    return bool((cluster.get(AUTOPILOT_KEY) or {}).get(AUTOPILOT_ENABLED_KEY))


def _grade_surge(out: dict, cluster: dict, surge_pools: list[tuple[dict, dict]], pools_parsed: dict, target, nodes, nodes_error) -> None:
    """The surge halves over the pools whose upgrade creates surge nodes: each pool below the
    target with accelerators is graded on its reservation, and each Pod range on its free
    blocks against the surge nodes the pools drawing on it need."""
    if not surge_pools:
        return
    if target is None:
        out["unknown"].append(UNKNOWN_SURGE_NO_TARGET.format(pools=LIST_SEPARATOR.join(p.get("name", "") for p, _ in surge_pools)))
        return
    pending = []
    for pool, settings in surge_pools:
        version = pools_parsed.get(pool.get("name"))
        if version is None:
            out["unknown"].append(UNKNOWN_SURGE_VERSION.format(pool=pool.get("name", "")))
        elif version < target:
            pending.append((pool, settings))
    if not pending:
        return
    for pool, settings in pending:
        accelerators = pool_accelerators(pool)
        if not accelerators:
            continue
        name, text, reservation = pool.get("name", ""), accelerators_text(accelerators), pool_reservation(pool)
        if reservation:
            out["notes"].append(NOTE_ACCELERATOR_RESERVED.format(pool=name, accelerators=text, max_surge=settings["max_surge"], reservation=reservation))
        else:
            # A risk, not a blocker: the surge upgrade waits for the accelerator with the old
            # node serving (catalogue entry 2); the Pod-range shortfall below is the blocker.
            out["risks"].append({FINDING_KEY: FINDING_ACCELERATOR, "pool": name, "accelerators": text, "max_surge": settings["max_surge"], "reservation": None})
    by_range: dict = {}
    for pool, settings in pending:
        by_range.setdefault(pool_pod_range(pool, cluster), []).append((pool, settings))
    for range_text, members in by_range.items():
        pools_text = LIST_SEPARATOR.join(SURGE_POOL_TEXT.format(pool=p.get("name", ""), max_surge=s["max_surge"]) for p, s in members)
        if nodes_error:
            out["unknown"].append(UNKNOWN_POD_RANGE_UNREAD.format(pools=pools_text, kind=NODE_READ_KIND, error=nodes_error))
            continue
        if not range_text:
            out["unknown"].append(UNKNOWN_POD_RANGE_MISSING.format(pools=pools_text))
            continue
        pod_range = pod_network(range_text)
        if pod_range is None:
            out["unknown"].append(UNKNOWN_POD_RANGE_UNPARSABLE.format(pools=pools_text, range=range_text))
            continue
        prefix = min(pool_block_prefix(p, cluster, [n for n in nodes if node_pool(n) == p.get("name", "")]) for p, _ in members)
        need = sum(s["max_surge"] for _, s in members)
        free, held, total = free_blocks(pod_range, prefix, nodes)
        if free < need:
            out["blocking"].append(
                {
                    FINDING_KEY: FINDING_POD_RANGE,
                    "range": range_text,
                    "prefix": prefix,
                    "free": free,
                    "held": held,
                    "total": total,
                    "need": need,
                    "pools": [{"pool": p.get("name", ""), "max_surge": s["max_surge"]} for p, s in members],
                }
            )


def evaluate(cluster: dict, member: dict, read, target, context) -> dict:
    out = new_result()
    if _is_autopilot(cluster, context):
        return out
    pools_parsed = {p.get("name"): p.get("parsed") for p in ((context or {}).get(CONTEXT_POOLS) or []) if isinstance(p, dict)}
    items = read_items(read)
    nodes_error = read_error(read, NODE_READ_KIND)
    pods_error = read_error(read, POD_READ_KIND)
    nodes = None if nodes_error else items_of_kind(items, KIND_NODE)
    pods = active_pods(items)
    surge_pools = []
    for pool in cluster.get("nodePools") or []:
        if not isinstance(pool, dict):
            continue
        settings = pool_settings(pool)
        if surges(settings):
            surge_pools.append((pool, settings))
        if not removes_node_first(settings):
            continue
        name = pool.get("name", "")
        autoscaled, ceiling = autoscaler_ceiling(pool, cluster)
        pool_nodes = [n for n in nodes if node_pool(n) == name] if nodes is not None else None
        node_count = NODE_COUNT_UNREAD if pool_nodes is None else len(pool_nodes)
        at_ceiling = (not autoscaled) or (ceiling is not None and pool_nodes is not None and len(pool_nodes) >= ceiling)
        summary = _summary(name, settings, node_count, autoscaled, ceiling, at_ceiling)
        if target is None:
            out["unknown"].append(UNKNOWN_NO_TARGET.format(summary=summary))
            continue
        version = pools_parsed.get(name)
        if version is None:
            out["unknown"].append(UNKNOWN_VERSION.format(summary=summary))
            continue
        if version >= target:
            continue
        if nodes_error:
            out["unknown"].append(UNKNOWN_KIND_UNREAD.format(summary=summary, kind=NODE_READ_KIND, error=nodes_error))
            continue
        if pods_error:
            out["unknown"].append(UNKNOWN_KIND_UNREAD.format(summary=summary, kind=POD_READ_KIND, error=pods_error))
            continue
        if not pool_nodes:
            out["notes"].append(NOTE_EMPTY.format(summary=summary))
            continue
        measured = measure(name, pool_nodes, nodes, pods)
        if isinstance(measured, str):
            out["unknown"].append(UNKNOWN_QUANTITY.format(summary=summary, what=measured))
            continue
        if not measured["short"]:
            out["notes"].append(NOTE_HEADROOM.format(summary=summary))
            continue
        finding = {
            FINDING_KEY: FINDING_PLACEMENT,
            "pool": name,
            "strategy": settings["strategy"],
            "max_surge": settings["max_surge"],
            "max_unavailable": settings["max_unavailable"],
            "autoscaled": autoscaled,
            "ceiling": ceiling,
            "at_ceiling": at_ceiling,
            "node_count": len(pool_nodes),
            "summary": summary,
            **measured,
        }
        blocks = settings["strategy"] == STRATEGY_SURGE and settings["max_surge"] == 0 and settings["max_unavailable"] > 0 and at_ceiling
        (out["blocking"] if blocks else out["risks"]).append(finding)
    _grade_surge(out, cluster, surge_pools, pools_parsed, target, nodes, nodes_error)
    return out


def describe(finding: dict) -> str:
    kind = finding.get(FINDING_KEY, FINDING_PLACEMENT)
    if kind == FINDING_POD_RANGE:
        pools = LIST_SEPARATOR.join(SURGE_POOL_TEXT.format(pool=p["pool"], max_surge=p["max_surge"]) for p in finding["pools"])
        return POD_RANGE_FINDING_TEXT.format(range=finding["range"], free=finding["free"], prefix=finding["prefix"], used=finding["held"], total=finding["total"], need=finding["need"], pools=pools)
    if kind == FINDING_ACCELERATOR:
        return ACCELERATOR_FINDING_TEXT.format(pool=finding["pool"], accelerators=finding["accelerators"], max_surge=finding["max_surge"])
    workloads = LIST_SEPARATOR.join(
        WORKLOAD_TEXT.format(
            owner=w["owner"],
            cpu=format_cpu(w["cpu_m"]),
            memory=format_memory(w["memory_bytes"]),
            extended="".join(EXTENDED_TEXT.format(amount=amount, resource=resource) for resource, amount in sorted((w.get("extended") or {}).items()) if amount),
            pinned=WORKLOAD_PINNED_SUFFIX if w["pinned"] else "",
        )
        for w in finding["workloads"]
    )
    more = finding["stranded_count"] - len(finding["workloads"])
    if more > 0:
        workloads += MORE_WORKLOADS.format(count=more)
    also = ALSO_SHORT_TEXT.format(nodes=LIST_SEPARATOR.join(finding["other_short_nodes"])) if finding["other_short_nodes"] else ""
    return FINDING_TEXT.format(
        summary=finding["summary"],
        node=finding["node"],
        cpu=format_cpu(finding["stranded"]["cpu_m"]),
        memory=format_memory(finding["stranded"]["memory_bytes"]),
        room_cpu=format_cpu(finding["room"]["cpu_m"]),
        room_memory=format_memory(finding["room"]["memory_bytes"]),
        workloads=workloads,
        also=also,
    )
