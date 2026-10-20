"""
Entry 5 of docs/designs/upgrade-failure-catalogue.md, read before the window: the entry's
after-signal is an operation still open, and a cluster whose `status` is not `RUNNING`
(`RECONCILING`, `DEGRADED`, `ERROR`) accepts no node pool or cluster change. The rule reads
the cluster's `status` and, from the project's operations (the report's `operations` read),
the operations whose target is this cluster or one of its node pools: an `UPGRADE_NODES`, a
resize or a repair names the pool (`.../clusters/<name>/nodePools/<pool>`) and holds the
cluster's status as a cluster operation does. The holder is the newest of them whose status
is RUNNING, and its age is from its start time. A cluster not `RUNNING` is a blocker when the
holder is older than STALE_AFTER_SECONDS, naming the operation, its type and pool, its start
and its age; a younger holder is a note, a reconcile in progress, which is also why the
report's own "upgrade in flight" treatment of `RECONCILING` stands for the version findings.
A cluster not `RUNNING` with no RUNNING operation on it or its pools is held by something the
operations list does not show (a `DEGRADED` cluster needs user action, an `ERROR` one is
unusable, and the API puts the cause in `statusMessage`): a blocker quoting the cluster's
`statusMessage`, with the newest operation of any status as context and never as the holder.
A `RUNNING` cluster is a note naming its newest operation and that operation's status. The
action is the gke-upgrades skill's Stuck operation procedure: freeze changes; past a few
hours, open a support case quoting the operation id, or the status and its message when no
operation holds the cluster.
"""

from readiness_rules import (
    CONTEXT_AT,
    CONTEXT_CLUSTER_NAME,
    CONTEXT_LOCATION,
    OPERATION_NAME_KEY,
    OPERATION_START_KEY,
    OPERATION_STATUS_KEY,
    OPERATION_STATUS_RUNNING,
    OPERATION_TYPE_KEY,
    PROJECT_READ_OPERATIONS,
    SECONDS_PER_HOUR,
    age_seconds,
    format_duration,
    new_result,
    operation_target,
    parse_rfc3339,
    project_read,
)

RULE_ID = "cluster-status-age"
ENTRY = 5
CAN_BLOCK = True

STATUS_RUNNING = "RUNNING"
STATUS_KEY = "status"
STATUS_MESSAGE_KEY = "statusMessage"
CLUSTER_NAME_KEY = "name"
CLUSTER_LOCATION_KEY = "location"
# How long a RUNNING operation may hold a status other than RUNNING before it is a blocker
# rather than a reconcile in progress: a control-plane upgrade, a pool upgrade or a repair
# finishes well inside this; the wedged operations in the support history stood for days.
STALE_AFTER_SECONDS = 4 * SECONDS_PER_HOUR
NO_STATUS_MESSAGE = "none"
NO_OPERATION_LISTED = "none listed"
TYPE_ON_POOL_FORMAT = "{type} on {pool}"
ACTION = "freeze changes; past a few hours open a support case quoting the operation id: see the gke-upgrades skill's Stuck operation section"
NO_HOLDER_ACTION = "freeze changes; read the statusMessage and the cluster's conditions, and past a few hours open a support case quoting the status, its message and the newest operation id: see the gke-upgrades skill's Stuck operation section"
HELD_FINDING_FORMAT = "{cluster} has been {status} for {age} on {operation} ({type}, RUNNING since {started}), past the {threshold} threshold; {action}"
NO_HOLDER_FINDING_FORMAT = "{cluster} is {status} with no operation RUNNING on it or its node pools (statusMessage: {message}; newest operation: {newest}); {action}"
NEWEST_FORMAT = "{operation} ({type}, {op_status}, started {started}, {age} ago)"
RUNNING_NOTE_FORMAT = "status RUNNING; newest operation {newest}"
RUNNING_NO_OPERATION_NOTE = "status RUNNING; no operation of this cluster or its node pools is listed"
UNDER_THRESHOLD_NOTE_FORMAT = "status {status} for {age} on {operation} ({type}, RUNNING since {started}); under the {threshold} threshold a reconcile in progress is not a blocker"
OPERATIONS_NOT_READ_REASON = "status {status}; the operations were not read ({error}), so what holds it and for how long is unknown"
OPERATIONS_UNREAD_RUNNING_NOTE = "status RUNNING; the operations were not read ({error})"
OPERATIONS_NOT_A_LIST = "the operations read returned {type}, not a list"
START_UNPARSABLE_REASON = "status {status}; the RUNNING operation {operation} carries no readable startTime ({started!r}), so its age is unknown"


def cluster_operations(operations, name: str, location: str) -> list[tuple[dict, str | None]]:
    """(operation, pool name or None) for every operation whose target is the cluster `name`
    at `location` or one of its node pools."""
    out = []
    for operation in operations or []:
        mine, pool = operation_target(operation, name, location)
        if mine:
            out.append((operation, pool))
    return out


def newest_operation(operations: list[tuple[dict, str | None]]):
    """(operation, pool, started) for the operation with the latest parseable startTime;
    (None, None, None) for none."""
    best, best_pool, best_started = None, None, None
    for operation, pool in operations:
        started = parse_rfc3339(operation.get(OPERATION_START_KEY))
        if started is not None and (best_started is None or started > best_started):
            best, best_pool, best_started = operation, pool, started
    return best, best_pool, best_started


def _type_text(operation: dict, pool) -> str:
    kind = str(operation.get(OPERATION_TYPE_KEY) or "")
    return TYPE_ON_POOL_FORMAT.format(type=kind, pool=pool) if pool else kind


def _newest_fields(operation, pool, started, at) -> dict | None:
    """The context an operation gives a finding or a note: its id, type, status, start and age."""
    if operation is None:
        return None
    return {"operation": operation.get(OPERATION_NAME_KEY), "operation_type": _type_text(operation, pool), "status": str(operation.get(OPERATION_STATUS_KEY) or ""), "started": operation.get(OPERATION_START_KEY), "age_seconds": age_seconds(at, started)}


def _newest_text(fields) -> str:
    if not fields:
        return NO_OPERATION_LISTED
    return NEWEST_FORMAT.format(operation=fields["operation"], type=fields["operation_type"], op_status=fields["status"], started=fields["started"], age=format_duration(fields["age_seconds"]))


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
    at = context[CONTEXT_AT]
    mine = cluster_operations(operations, name, location)
    newest = _newest_fields(*newest_operation(mine), at)
    if running:
        out["notes"].append(RUNNING_NOTE_FORMAT.format(newest=_newest_text(newest)) if newest else RUNNING_NO_OPERATION_NOTE)
        return out
    in_flight = [(operation, pool) for operation, pool in mine if operation.get(OPERATION_STATUS_KEY) == OPERATION_STATUS_RUNNING]
    holder, holder_pool, holder_started = newest_operation(in_flight)
    if in_flight and holder is None:
        operation = in_flight[0][0]
        out["unknown"].append(START_UNPARSABLE_REASON.format(status=status, operation=operation.get(OPERATION_NAME_KEY), started=operation.get(OPERATION_START_KEY)))
        return out
    message = str(cluster.get(STATUS_MESSAGE_KEY) or "") or None
    if holder is None:
        out["blocking"].append({"cluster": name, "status": status, "status_message": message, "operation": None, "operation_type": None, "pool": None, "started": None, "age_seconds": None, "newest": newest, "threshold_seconds": STALE_AFTER_SECONDS, "action": NO_HOLDER_ACTION})
        return out
    age = age_seconds(at, holder_started)
    fields = {"operation": holder.get(OPERATION_NAME_KEY), "type": _type_text(holder, holder_pool), "started": holder.get(OPERATION_START_KEY), "age": format_duration(age), "threshold": format_duration(STALE_AFTER_SECONDS), "status": status}
    if age <= STALE_AFTER_SECONDS:
        out["notes"].append(UNDER_THRESHOLD_NOTE_FORMAT.format(**fields))
    else:
        out["blocking"].append({"cluster": name, "status": status, "status_message": message, "operation": fields["operation"], "operation_type": fields["type"], "pool": holder_pool, "started": fields["started"], "age_seconds": age, "newest": None, "threshold_seconds": STALE_AFTER_SECONDS, "action": ACTION})
    return out


def describe(finding: dict) -> str:
    if finding.get("operation") is None:
        return NO_HOLDER_FINDING_FORMAT.format(cluster=finding["cluster"], status=finding["status"], message=finding.get("status_message") or NO_STATUS_MESSAGE, newest=_newest_text(finding.get("newest")), action=finding["action"])
    return HELD_FINDING_FORMAT.format(cluster=finding["cluster"], status=finding["status"], age=format_duration(finding["age_seconds"]), operation=finding["operation"], type=finding["operation_type"], started=finding["started"], threshold=format_duration(finding["threshold_seconds"]), action=finding["action"])
