"""
Entry 5 of docs/designs/upgrade-failure-catalogue.md, read before the window: the entry's
after-signal is an operation still open, and a cluster whose `status` is not `RUNNING`
(`RECONCILING`, `DEGRADED`, `ERROR`) is held by one. Such a cluster accepts no node pool or
cluster change, and only the provider can end an operation that has wedged. The rule reads
the cluster's `status` and, from the project's operations (the report's `operations` read),
the newest operation whose target is this cluster, and files one blocker when the status is
not `RUNNING` and that operation started more than STALE_AFTER_SECONDS ago, naming the
operation, its type, its start and its age; a status that has stood for less is a reconcile
in progress, noted and not a blocker, which is also why the report's own "upgrade in
flight" treatment of `RECONCILING` stands for the version findings. A cluster not `RUNNING`
with no operation listed for it at all has stood longer than the operations list reaches
and is a blocker too. The action is the gke-upgrades skill's Stuck operation procedure:
freeze changes; past a few hours, open a support case quoting the operation id.
"""

from readiness_rules import (
    CONTEXT_AT,
    CONTEXT_CLUSTER_NAME,
    CONTEXT_LOCATION,
    PATH_SEPARATOR,
    PROJECT_READ_OPERATIONS,
    SECONDS_PER_HOUR,
    age_seconds,
    format_duration,
    new_result,
    parse_rfc3339,
    project_read,
)

RULE_ID = "cluster-status-age"
ENTRY = 5
CAN_BLOCK = True

STATUS_RUNNING = "RUNNING"
STATUS_KEY = "status"
CLUSTER_NAME_KEY = "name"
CLUSTER_LOCATION_KEY = "location"
# How long a status other than RUNNING may stand before it is a blocker rather than a
# reconcile in progress: a control-plane upgrade or repair finishes well inside this; the
# wedged operations in the support history stood for days.
STALE_AFTER_SECONDS = 4 * SECONDS_PER_HOUR
# The operation record's fields, as `gcloud container operations list` returns them, and how
# its `targetLink` names a cluster: `.../zones/<zone>/clusters/<name>` or `.../locations/<location>/clusters/<name>`.
OPERATION_NAME_KEY = "name"
OPERATION_TYPE_KEY = "operationType"
OPERATION_START_KEY = "startTime"
OPERATION_TARGET_KEY = "targetLink"
CLUSTERS_SEGMENT = "clusters"
TARGET_TAIL_PARTS = 4  # <location>/clusters/<name> and the segment naming the location
ACTION = "freeze changes; past a few hours open a support case quoting the operation id: see the gke-upgrades skill's Stuck operation section"
FINDING_FORMAT = "{cluster} has been {status} for {age} on {operation} ({type}, started {started}), past the {threshold} threshold; {action}"
FINDING_NO_OPERATION_FORMAT = "{cluster} has been {status} longer than the operations list reaches (no operation of this cluster is listed); {action}"
RUNNING_NOTE_FORMAT = "status RUNNING; newest operation {operation} ({type}, started {started}, {age} ago)"
RUNNING_NO_OPERATION_NOTE = "status RUNNING; no operation of this cluster is listed"
UNDER_THRESHOLD_NOTE_FORMAT = "status {status} for {age} on {operation} ({type}, started {started}); under the {threshold} threshold a reconcile in progress is not a blocker"
OPERATIONS_NOT_READ_REASON = "status {status}; the operations were not read ({error}), so how long it has stood is unknown"
OPERATIONS_UNREAD_RUNNING_NOTE = "status RUNNING; the operations were not read ({error})"
OPERATIONS_NOT_A_LIST = "the operations read returned {type}, not a list"
START_UNPARSABLE_NOTE = "status {status}; the newest operation {operation} carries no readable startTime ({started!r}), so its age is unknown"


def cluster_operations(operations, name: str, location: str) -> list[dict]:
    """The operations whose target is the cluster `name` at `location`."""
    out = []
    for operation in operations or []:
        if not isinstance(operation, dict):
            continue
        parts = str(operation.get(OPERATION_TARGET_KEY) or "").rstrip(PATH_SEPARATOR).split(PATH_SEPARATOR)
        if len(parts) < TARGET_TAIL_PARTS:
            continue
        if parts[-1] == name and parts[-2] == CLUSTERS_SEGMENT and parts[-3] == location:
            out.append(operation)
    return out


def newest_operation(operations: list[dict]):
    """(operation, started) for the operation with the latest parseable startTime; (None, None) for none."""
    best, best_started = None, None
    for operation in operations:
        started = parse_rfc3339(operation.get(OPERATION_START_KEY))
        if started is not None and (best_started is None or started > best_started):
            best, best_started = operation, started
    return best, best_started


def evaluate(cluster: dict, member: dict, read, target, context) -> dict:
    out = new_result()
    name = str(cluster.get(CLUSTER_NAME_KEY) or context.get(CONTEXT_CLUSTER_NAME) or "")
    location = str(cluster.get(CLUSTER_LOCATION_KEY) or context.get(CONTEXT_LOCATION) or "")
    status = str(cluster.get(STATUS_KEY) or "")
    running = status == STATUS_RUNNING
    operations, error = project_read(context, PROJECT_READ_OPERATIONS)
    if error is not None:
        (out["notes"] if running else out["unknown"]).append((OPERATIONS_UNREAD_RUNNING_NOTE if running else OPERATIONS_NOT_READ_REASON).format(status=status, error=error))
        return out
    if not isinstance(operations, list):
        out["unknown"].append(OPERATIONS_NOT_A_LIST.format(type=type(operations).__name__))
        return out
    mine = cluster_operations(operations, name, location)
    operation, started = newest_operation(mine)
    if operation is None:
        if running:
            out["notes"].append(RUNNING_NO_OPERATION_NOTE)
        elif mine:
            out["unknown"].append(START_UNPARSABLE_NOTE.format(status=status, operation=mine[0].get(OPERATION_NAME_KEY), started=mine[0].get(OPERATION_START_KEY)))
        else:
            out["blocking"].append({"cluster": name, "status": status, "operation": None, "operation_type": None, "started": None, "age_seconds": None, "threshold_seconds": STALE_AFTER_SECONDS, "action": ACTION})
        return out
    age = age_seconds(context[CONTEXT_AT], started)
    fields = {"operation": operation.get(OPERATION_NAME_KEY), "type": operation.get(OPERATION_TYPE_KEY), "started": operation.get(OPERATION_START_KEY), "age": format_duration(age), "threshold": format_duration(STALE_AFTER_SECONDS), "status": status}
    if running:
        out["notes"].append(RUNNING_NOTE_FORMAT.format(**fields))
    elif age <= STALE_AFTER_SECONDS:
        out["notes"].append(UNDER_THRESHOLD_NOTE_FORMAT.format(**fields))
    else:
        out["blocking"].append({"cluster": name, "status": status, "operation": fields["operation"], "operation_type": fields["type"], "started": fields["started"], "age_seconds": age, "threshold_seconds": STALE_AFTER_SECONDS, "action": ACTION})
    return out


def describe(finding: dict) -> str:
    if finding.get("operation") is None:
        return FINDING_NO_OPERATION_FORMAT.format(cluster=finding["cluster"], status=finding["status"], action=finding["action"])
    return FINDING_FORMAT.format(cluster=finding["cluster"], status=finding["status"], age=format_duration(finding["age_seconds"]), operation=finding["operation"], type=finding["operation_type"], started=finding["started"], threshold=format_duration(finding["threshold_seconds"]), action=finding["action"])
