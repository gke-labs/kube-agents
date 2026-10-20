"""
Entry 2 of docs/designs/upgrade-failure-catalogue.md, on the control plane's side: an
automated control-plane upgrade, a node upgrade or a repair needs compute capacity in the
location when it fires, the provider does not check capacity first, and the agent cannot
read zone capacity. It can read the cluster's recent operations. The rule reads the project's
operations (the report's `operations` read) and files one risk per `DONE` operation of type
UPGRADE_MASTER, UPGRADE_NODES or REPAIR_CLUSTER on this cluster in the last LOOKBACK_DAYS
whose `error` or `statusMessage` names capacity (CAPACITY_MARKERS), with the operation's
date and the cluster's maintenance window, which is where the odds are moved: a window in an
off-peak hour for the region, and the upgrade retried in it. A lagging signal by nature: the
finding explains the last failure and lowers the odds of the next. Never a blocker, so
`CAN_BLOCK` is False and a failed operations read is reported in the note.
"""

from readiness_rules import (
    CONTEXT_AT,
    CONTEXT_LOCATION,
    NOTE_EXCERPT_CHARS,
    PATH_SEPARATOR,
    PROJECT_READ_OPERATIONS,
    SECONDS_PER_DAY,
    new_result,
    one_line,
    parse_rfc3339,
    project_read,
)

RULE_ID = "control-plane-upgrade-history"
ENTRY = 2
CAN_BLOCK = False

# How far back the operations are read: GKE lists a project's operations for a bounded time,
# and a failure older than this says little about the next window.
LOOKBACK_DAYS = 90
LOOKBACK_SECONDS = LOOKBACK_DAYS * SECONDS_PER_DAY
OPERATION_TYPES = ("UPGRADE_MASTER", "UPGRADE_NODES", "REPAIR_CLUSTER")
STATUS_DONE = "DONE"
# What a capacity failure's message carries, matched without case: the Compute Engine
# phrasing, the stockout error codes, and the quota and resource shortfalls.
CAPACITY_MARKERS = ("does not have enough resources", "resource_pool_exhausted", "stockout", "insufficient")
# The operation record's fields.
OPERATION_NAME_KEY = "name"
OPERATION_TYPE_KEY = "operationType"
OPERATION_STATUS_KEY = "status"
OPERATION_START_KEY = "startTime"
OPERATION_TARGET_KEY = "targetLink"
OPERATION_ERROR_KEY = "error"
OPERATION_ERROR_MESSAGE_KEY = "message"
OPERATION_STATUS_MESSAGE_KEY = "statusMessage"
CLUSTERS_SEGMENT = "clusters"
TARGET_TAIL_PARTS = 4
# The cluster's maintenance window, as the record spells it.
MAINTENANCE_POLICY_KEY = "maintenancePolicy"
WINDOW_KEY = "window"
DAILY_WINDOW_KEY = "dailyMaintenanceWindow"
RECURRING_WINDOW_KEY = "recurringWindow"
WINDOW_START_KEY = "startTime"
WINDOW_DURATION_KEY = "duration"
RECURRENCE_KEY = "recurrence"
WINDOW_NONE = "none"
DAILY_WINDOW_FORMAT = "daily at {start} UTC for {duration}"
RECURRING_WINDOW_FORMAT = "recurring {recurrence} from {start}"
DATE_CHARS = 10  # YYYY-MM-DD of an RFC 3339 start time
ACTION = "set a maintenance window in an off-peak hour for the region and retry the upgrade in it; keep upgrade notifications consumed so a failed attempt is a message the same day"
FINDING_FORMAT = "{type} on {date} failed on compute capacity in {location} ({message}); maintenance window: {window}; {action}"
NOTE_FORMAT = "{failures} capacity failure(s) among {operations} upgrade or repair operation(s) in the last {days} days; maintenance window {window}"
OPERATIONS_NOT_READ_REASON = "the operations were not read ({error}), so the upgrade history is unknown"
OPERATIONS_NOT_A_LIST = "the operations read returned {type}, not a list"


def maintenance_window(cluster: dict) -> str:
    window = ((cluster.get(MAINTENANCE_POLICY_KEY) or {}).get(WINDOW_KEY)) or {}
    daily = window.get(DAILY_WINDOW_KEY)
    if isinstance(daily, dict) and daily.get(WINDOW_START_KEY):
        return DAILY_WINDOW_FORMAT.format(start=daily.get(WINDOW_START_KEY), duration=daily.get(WINDOW_DURATION_KEY) or "")
    recurring = window.get(RECURRING_WINDOW_KEY)
    if isinstance(recurring, dict) and recurring.get(RECURRENCE_KEY):
        return RECURRING_WINDOW_FORMAT.format(recurrence=recurring.get(RECURRENCE_KEY), start=((recurring.get(WINDOW_KEY) or {}).get(WINDOW_START_KEY)) or "")
    return WINDOW_NONE


def failure_message(operation: dict) -> str:
    error = operation.get(OPERATION_ERROR_KEY)
    message = error.get(OPERATION_ERROR_MESSAGE_KEY) if isinstance(error, dict) else None
    return str(message or operation.get(OPERATION_STATUS_MESSAGE_KEY) or "")


def names_capacity(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in CAPACITY_MARKERS)


def _targets_cluster(operation: dict, name: str, location: str) -> bool:
    parts = str(operation.get(OPERATION_TARGET_KEY) or "").rstrip(PATH_SEPARATOR).split(PATH_SEPARATOR)
    return len(parts) >= TARGET_TAIL_PARTS and parts[-1] == name and parts[-2] == CLUSTERS_SEGMENT and parts[-3] == location


def evaluate(cluster: dict, member: dict, read, target, context) -> dict:
    out = new_result()
    operations, error = project_read(context, PROJECT_READ_OPERATIONS)
    if error is not None:
        out["unknown"].append(OPERATIONS_NOT_READ_REASON.format(error=error))
        return out
    if not isinstance(operations, list):
        out["unknown"].append(OPERATIONS_NOT_A_LIST.format(type=type(operations).__name__))
        return out
    name = str(cluster.get("name") or "")
    location = str(cluster.get("location") or context.get(CONTEXT_LOCATION) or "")
    window = maintenance_window(cluster)
    at = context[CONTEXT_AT]
    considered = 0
    for operation in operations:
        if not isinstance(operation, dict) or operation.get(OPERATION_TYPE_KEY) not in OPERATION_TYPES or operation.get(OPERATION_STATUS_KEY) != STATUS_DONE:
            continue
        if not _targets_cluster(operation, name, location):
            continue
        started = parse_rfc3339(operation.get(OPERATION_START_KEY))
        if started is None or (at - started).total_seconds() > LOOKBACK_SECONDS:
            continue
        considered += 1
        message = failure_message(operation)
        if message and names_capacity(message):
            out["risks"].append({
                "operation": operation.get(OPERATION_NAME_KEY),
                "operation_type": operation.get(OPERATION_TYPE_KEY),
                "date": str(operation.get(OPERATION_START_KEY))[:DATE_CHARS],
                "location": location,
                "message": one_line(message, NOTE_EXCERPT_CHARS),
                "window": window,
                "action": ACTION,
            })
    out["notes"].append(NOTE_FORMAT.format(failures=len(out["risks"]), operations=considered, days=LOOKBACK_DAYS, window=window))
    return out


def describe(finding: dict) -> str:
    return FINDING_FORMAT.format(type=finding["operation_type"], date=finding["date"], location=finding["location"], message=finding["message"], window=finding["window"], action=finding["action"])
