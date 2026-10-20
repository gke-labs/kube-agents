"""
Entry 22 of docs/designs/upgrade-failure-catalogue.md: a Node object outlives its virtual
machine.

When an operation that deletes a node's instance is interrupted and GKE's node lifecycle
controller does not collect the Node object, the object stays, usually `Ready` `Unknown` and
cordoned, and the next node pool upgrade waits on it. The rule joins two inventories on one
identifier: the cluster's Nodes (the report's node read, each with its `spec.providerID`,
`gce://<project>/<zone>/<instance>`) and the project's compute instances (the report's
`instances` read, name and zone). A Node whose instance is in neither the zone its providerID
names nor, for a Node with no providerID, the project at all under the Node's own name (a GKE
node is named for its instance) is an orphan: one blocker per Node, naming its Ready condition
and whether it is cordoned, with the one action, `kubectl delete node <name>`. The instance
list is read once per project before the members' own reads, so a Node that is `Ready` `True`
and not cordoned with no instance in the list joined the cluster after the list was made (a
scale-up, a surge node) rather than losing its instance: it is a note naming the list's read
time, not a blocker, and the next run grades it. The note carries the count either way, so a
clean cluster reads as graded rather than silent.
"""

from readiness_rules import (
    CONDITION_TRUE,
    KIND_NODE,
    LIST_SEPARATOR,
    NODE_READ_KIND,
    PATH_SEPARATOR,
    PROJECT_READ_INSTANCES,
    items_of_kind,
    new_result,
    node_instance,
    node_provider_id,
    node_ready,
    project_read,
    project_read_at,
    read_error,
    read_items,
)

RULE_ID = "orphan-node-objects"
ENTRY = 22
CAN_BLOCK = True

# The instance record's fields, as `gcloud compute instances list --format=json(name,zone,status)` returns them.
INSTANCE_NAME_KEY = "name"
INSTANCE_ZONE_KEY = "zone"
ACTION_FORMAT = "kubectl delete node {node}"
FINDING_FORMAT = (
    "Node {node} names no instance in the project (providerID {provider_id}; Ready={ready}; unschedulable={unschedulable}): "
    "a pool upgrade waits on it; delete the Node object: `{action}`"
)
NO_PROVIDER_ID = "none"
UNKNOWN_READY = "unknown"
COUNT_NOTE_FORMAT = "{orphans} orphan Node(s) ({nodes} nodes, {instances} instances in the project)"
# A Ready, uncordoned Node the instance list does not carry: the list predates the node read.
LATER_NODES_NOTE_FORMAT = "{later} Ready, uncordoned Node(s) not in the instance list read at {read_at} ({names}): a node that joined after the list is not an orphan; the next run grades it"
NODES_NOT_READ_REASON = "the Nodes were not read ({error}), so no Node could be checked against the instances"
INSTANCES_NOT_A_LIST = "the instances read returned {type}, not a list"


def instance_index(instances) -> tuple[set[tuple[str, str]], set[str]]:
    """({(zone, name)}, {name}) of the project's instances; the zone is the URL's last component."""
    by_zone, names = set(), set()
    for instance in instances or []:
        if not isinstance(instance, dict):
            continue
        name = str(instance.get(INSTANCE_NAME_KEY) or "")
        if not name:
            continue
        zone = str(instance.get(INSTANCE_ZONE_KEY) or "").rstrip(PATH_SEPARATOR).rsplit(PATH_SEPARATOR, 1)[-1]
        by_zone.add((zone, name))
        names.add(name)
    return by_zone, names


def is_live(node: dict, by_zone: set, names: set) -> bool:
    zone, name = node_instance(node)
    if zone:
        return (zone, name) in by_zone
    return name in names


def joined_later(node: dict) -> bool:
    """A Ready, uncordoned Node with no instance in the list: one the list predates, not an orphan."""
    return node_ready(node) == CONDITION_TRUE and not (node.get("spec") or {}).get("unschedulable")


def evaluate(cluster: dict, member: dict, read, target, context) -> dict:
    out = new_result()
    error = read_error(read, NODE_READ_KIND)
    if error:
        out["unknown"].append(NODES_NOT_READ_REASON.format(error=error))
        return out
    instances, read_failure = project_read(context, PROJECT_READ_INSTANCES)
    if read_failure is not None:
        out["unknown"].append(read_failure)
        return out
    if not isinstance(instances, list):
        out["unknown"].append(INSTANCES_NOT_A_LIST.format(type=type(instances).__name__))
        return out
    by_zone, names = instance_index(instances)
    nodes = items_of_kind(read_items(read), KIND_NODE)
    later = []
    for node in nodes:
        if is_live(node, by_zone, names):
            continue
        zone, name = node_instance(node)
        if joined_later(node):
            later.append(name)
            continue
        out["blocking"].append({
            "node": name,
            "provider_id": node_provider_id(node) or NO_PROVIDER_ID,
            "zone": zone,
            "ready": node_ready(node) or UNKNOWN_READY,
            "unschedulable": bool((node.get("spec") or {}).get("unschedulable")),
            "action": ACTION_FORMAT.format(node=name),
        })
    out["notes"].append(COUNT_NOTE_FORMAT.format(orphans=len(out["blocking"]), nodes=len(nodes), instances=len(names)))
    if later:
        out["notes"].append(LATER_NODES_NOTE_FORMAT.format(later=len(later), read_at=project_read_at(context, PROJECT_READ_INSTANCES), names=LIST_SEPARATOR.join(later)))
    return out


def describe(finding: dict) -> str:
    return FINDING_FORMAT.format(node=finding["node"], provider_id=finding["provider_id"], ready=finding["ready"], unschedulable=str(finding["unschedulable"]).lower(), action=finding["action"])
