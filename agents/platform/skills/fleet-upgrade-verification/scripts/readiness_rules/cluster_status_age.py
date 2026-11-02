"""
Entry 24 of docs/designs/upgrade-failure-catalogue.md: a cluster held in a long reconcile, which
is entry 5's after-signal, an operation still open, read before the window. A cluster whose
`status` is not `RUNNING` (`RECONCILING`, `DEGRADED`, `ERROR`) accepts no node pool or cluster
change, so it blocks every upgrade until the control plane settles. The rule reads
the cluster's `status` and, from the project's operations (the report's `operations` read),
the operations whose target is this cluster or one of its node pools: an `UPGRADE_NODES`, a
resize or a repair names the pool (`.../clusters/<name>/nodePools/<pool>`) and holds the
cluster's status as a cluster operation does. The holder is the newest of them whose status
is RUNNING, and its age is from its start time. A cluster not `RUNNING` is a blocker when the
holder is older than STALE_AFTER_SECONDS, naming the operation, its type and pool, its start
and its age; a younger holder is a note, a reconcile in progress, which is also why the
report's own "upgrade in flight" treatment of `RECONCILING` stands for the version findings.
A cluster not `RUNNING` with no RUNNING operation in the project's list is read again, through
the report's runner (the rule's two reads of its own): the clusters list is made before any
member is graded and the operations list at the project's first member, so an operation that
ended in between leaves a list status the cluster no longer has, and one that started in between
is in no list the rule saw. First `gcloud container clusters describe`: `RUNNING` now is a note,
a failed describe is `unknown`. Then, for a status still not `RUNNING`, `gcloud container
operations list` for this cluster alone: a RUNNING operation younger than the threshold is the
note, an older one the blocker naming it, a failed list `unknown`, and none at all is a status
held by something no operation shows (a `DEGRADED` cluster needs user action, an `ERROR` one is
unusable, and the API puts the cause in `statusMessage`): a blocker quoting the cluster's
`statusMessage` as described, with the newest operation of any status as context and never as
the holder.
A `RUNNING` cluster is a note naming its newest operation and that operation's status. The
action is the gke-upgrades skill's Stuck operation procedure: freeze changes; past a few
hours, open a support case quoting the operation id, or the status and its message when no
operation holds the cluster.
"""

import json

from readiness_rules import (
    CONTEXT_AT,
    CONTEXT_CLUSTER_NAME,
    CONTEXT_LOCATION,
    CONTEXT_PROJECT,
    CONTEXT_RUN_CMD,
    CONTEXT_TIMEOUT_SECONDS,
    ERROR_EXCERPT_CHARS,
    GCLOUD,
    HOLDS_BOTH,
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
    one_line,
    operation_target,
    parse_rfc3339,
    project_read,
)

RULE_ID = "cluster-status-age"
ENTRY = 24
CAN_BLOCK = True
HOLDS = HOLDS_BOTH  # a held cluster takes no change: neither move goes through

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
# The rule's one read of its own: the cluster's status as it stands now, for a cluster the list
# showed not RUNNING with no RUNNING operation on it or its pools. The clusters list is made
# before any member is graded and the operations list at the project's first member, so an
# operation that ended in between leaves a list status the cluster no longer has.
DESCRIBE_COMMAND = ("container", "clusters", "describe")
DESCRIBE_LOCATION_FLAG = "--location"
DESCRIBE_PROJECT_FLAG = "--project"
DESCRIBE_FORMAT_FLAG = "--format=json(status,statusMessage)"
NO_RUNNER = "the context carries no command runner"
DESCRIBE_EXIT_FORMAT = "exit {rc}"
DESCRIBE_NO_STATUS = "the response carries no status"
DESCRIBE_FAILED_REASON = "status {status} at list time with no RUNNING operation on it or its node pools; `gcloud container clusters describe` could not confirm it ({error}), so whether it still stands is unknown"
# The rule's second read: this cluster's operations now, for a status the describe confirmed.
RELIST_COMMAND = ("container", "operations", "list")
RELIST_PROJECT_FLAG_FORMAT = "--project={project}"
RELIST_LOCATION_FLAG_FORMAT = "--location={location}"
RELIST_FILTER_FLAG_FORMAT = "--filter=targetLink~/clusters/{name}($|/nodePools/)"
RELIST_FORMAT_FLAG = "--format=json(name,operationType,status,startTime,endTime,targetLink)"
RELIST_FAILED_REASON = "status {status} now with no RUNNING operation in the project's list; `gcloud container operations list` for the cluster could not say what holds it ({error}), so that is unknown"
SETTLED_RUNNING_NOTE_FORMAT = "status RUNNING now ({listed} at list time; newest operation {newest})"
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


def describe_status(context, name: str, location: str) -> tuple[str | None, str | None, str | None]:
    """(status, statusMessage, error) of the cluster as `gcloud container clusters describe`
    reports it now, through the context's runner; (None, None, why) when it could not say."""
    run = context.get(CONTEXT_RUN_CMD)
    if run is None:
        return None, None, NO_RUNNER
    cmd = [GCLOUD, *DESCRIBE_COMMAND, name, DESCRIBE_LOCATION_FLAG, location, DESCRIBE_PROJECT_FLAG, str(context.get(CONTEXT_PROJECT) or ""), DESCRIBE_FORMAT_FLAG]
    timeout = context.get(CONTEXT_TIMEOUT_SECONDS)
    rc, stdout, stderr = run(cmd, timeout) if timeout else run(cmd)
    if rc != 0:
        return None, None, one_line(stderr or DESCRIBE_EXIT_FORMAT.format(rc=rc), ERROR_EXCERPT_CHARS)
    try:
        record = json.loads(stdout)
    except ValueError as e:
        return None, None, one_line(str(e), ERROR_EXCERPT_CHARS)
    if not isinstance(record, dict) or not record.get(STATUS_KEY):
        return None, None, DESCRIBE_NO_STATUS
    return str(record.get(STATUS_KEY)), str(record.get(STATUS_MESSAGE_KEY) or "") or None, None


def list_operations(context, name: str, location: str) -> tuple[list | None, str | None]:
    """(this cluster's operations as `gcloud container operations list` returns them now, None)
    through the context's runner, or (None, why) when it could not say."""
    run = context.get(CONTEXT_RUN_CMD)
    if run is None:
        return None, NO_RUNNER
    cmd = [GCLOUD, *RELIST_COMMAND, RELIST_PROJECT_FLAG_FORMAT.format(project=context.get(CONTEXT_PROJECT) or ""), RELIST_LOCATION_FLAG_FORMAT.format(location=location), RELIST_FILTER_FLAG_FORMAT.format(name=name), RELIST_FORMAT_FLAG]
    timeout = context.get(CONTEXT_TIMEOUT_SECONDS)
    rc, stdout, stderr = run(cmd, timeout) if timeout else run(cmd)
    if rc != 0:
        return None, one_line(stderr or DESCRIBE_EXIT_FORMAT.format(rc=rc), ERROR_EXCERPT_CHARS)
    try:
        operations = json.loads(stdout) if stdout.strip() else []
    except ValueError as e:
        return None, one_line(str(e), ERROR_EXCERPT_CHARS)
    if not isinstance(operations, list):
        return None, OPERATIONS_NOT_A_LIST.format(type=type(operations).__name__)
    return operations, None


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
    holder, holder_pool, holder_started, unparsable = _running_holder(mine)
    if unparsable is not None:
        out["unknown"].append(START_UNPARSABLE_REASON.format(status=status, operation=unparsable.get(OPERATION_NAME_KEY), started=unparsable.get(OPERATION_START_KEY)))
        return out
    message = str(cluster.get(STATUS_MESSAGE_KEY) or "") or None
    listed_status = status
    if holder is None:
        # Nothing RUNNING in the project's list: the list status may be older than the operation that
        # ended since. Describe the cluster once.
        fresh_status, fresh_message, error = describe_status(context, name, location)
        if error is not None:
            out["unknown"].append(DESCRIBE_FAILED_REASON.format(status=status, error=error))
            return out
        if fresh_status == STATUS_RUNNING:
            out["notes"].append(SETTLED_RUNNING_NOTE_FORMAT.format(listed=status, newest=_newest_text(newest)))
            return out
        # Still not RUNNING: the project's list may predate the operation holding it now. List this
        # cluster's operations once, and grade from that list.
        status, message = fresh_status, fresh_message or message
        listed_now, list_error = list_operations(context, name, location)
        if list_error is not None:
            out["unknown"].append(RELIST_FAILED_REASON.format(status=status, error=list_error))
            return out
        mine = cluster_operations(listed_now, name, location)
        newest = _newest_fields(*newest_operation(mine), at) or newest
        holder, holder_pool, holder_started, unparsable = _running_holder(mine)
        if unparsable is not None:
            out["unknown"].append(START_UNPARSABLE_REASON.format(status=status, operation=unparsable.get(OPERATION_NAME_KEY), started=unparsable.get(OPERATION_START_KEY)))
            return out
        if holder is None:
            out["blocking"].append({"cluster": name, "status": status, "listed_status": listed_status, "status_message": message, "operation": None, "operation_type": None, "pool": None, "started": None, "age_seconds": None, "newest": newest, "threshold_seconds": STALE_AFTER_SECONDS, "action": NO_HOLDER_ACTION})
            return out
    age = age_seconds(at, holder_started)
    fields = {"operation": holder.get(OPERATION_NAME_KEY), "type": _type_text(holder, holder_pool), "started": holder.get(OPERATION_START_KEY), "age": format_duration(age), "threshold": format_duration(STALE_AFTER_SECONDS), "status": status}
    if age <= STALE_AFTER_SECONDS:
        out["notes"].append(UNDER_THRESHOLD_NOTE_FORMAT.format(**fields))
    else:
        out["blocking"].append({"cluster": name, "status": status, "listed_status": listed_status, "status_message": message, "operation": fields["operation"], "operation_type": fields["type"], "pool": holder_pool, "started": fields["started"], "age_seconds": age, "newest": None, "threshold_seconds": STALE_AFTER_SECONDS, "action": ACTION})
    return out


def _running_holder(operations: list[tuple[dict, str | None]]):
    """(holder, pool, started, None) for the newest RUNNING operation among `operations`, or
    (None, None, None, operation) when one is RUNNING with no readable startTime."""
    in_flight = [(operation, pool) for operation, pool in operations if operation.get(OPERATION_STATUS_KEY) == OPERATION_STATUS_RUNNING]
    holder, pool, started = newest_operation(in_flight)
    if in_flight and holder is None:
        return None, None, None, in_flight[0][0]
    return holder, pool, started, None


def describe(finding: dict) -> str:
    if finding.get("operation") is None:
        return NO_HOLDER_FINDING_FORMAT.format(cluster=finding["cluster"], status=finding["status"], message=finding.get("status_message") or NO_STATUS_MESSAGE, newest=_newest_text(finding.get("newest")), action=finding["action"])
    return HELD_FINDING_FORMAT.format(cluster=finding["cluster"], status=finding["status"], age=format_duration(finding["age_seconds"]), operation=finding["operation"], type=finding["operation_type"], started=finding["started"], threshold=format_duration(finding["threshold_seconds"]), action=finding["action"])
