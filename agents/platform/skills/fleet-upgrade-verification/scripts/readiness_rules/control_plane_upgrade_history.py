"""
Entry 23 of docs/designs/upgrade-failure-catalogue.md: a control-plane upgrade or resize fails
on regional compute capacity. An automated control-plane upgrade, a node upgrade or a repair
needs compute capacity in the location when it fires, the provider does not check capacity first, and the agent cannot
read zone capacity. It can read the cluster's recent operations. The rule reads the project's
operations (the report's `operations` read) and files each `DONE` operation of type
UPGRADE_MASTER, UPGRADE_NODES or REPAIR_CLUSTER on this cluster or on one of its node pools
(an UPGRADE_NODES names the pool, `.../clusters/<name>/nodePools/<pool>`) in the last
LOOKBACK_DAYS whose `error` or `statusMessage` names capacity (CAPACITY_MARKERS), with the
operation's date and the cluster's maintenance window, which is where the odds are moved: a
window in an off-peak hour for the region, and the upgrade retried in it.

The rule, stated once. A capacity failure in the last LOOKBACK_DAYS is a risk when the cluster
has no maintenance window or its window opens inside the region's busy hours, with the action
to set a maintenance window in an off-peak hour for the region and retry the upgrade in it
(SET_WINDOW_ACTION); it is a note, not a risk, when the window opens in an off-peak hour, with
the action to retry the upgrade in that window (RETRY_ACTION_FORMAT). Both texts keep the
failure on record: it is history for LOOKBACK_DAYS. Busy hours are BUSY_HOURS_START up to
BUSY_HOURS_END in the region's standard local time, the working day and the early evening,
when the region's tenants draw its capacity. The window's opening, which GKE spells in UTC, is
placed in the region's standard time through REGION_UTC_OFFSET_HOURS, the provider's regions
with their cities' standard offsets: no daylight-saving shift, so a window's grade does not
change with the clock, at the price of one hour's error at the band's edges for half the year.
The opening hour alone is read, not the days or the length: GKE starts the attempt at the
window's opening, and a control-plane attempt is over well inside the hour. A window whose
opening the rule cannot place (a region REGION_UTC_OFFSET_HOURS does not list, or a start time
it cannot read) keeps the failure a risk with the set-window action and files one unknown
naming the gap, which the note column reports. A lagging signal by nature: the finding
explains the last failure and lowers the odds of the next. Never a blocker, so `CAN_BLOCK` is
False and a failed operations read is reported in the note.
"""

import re

from readiness_rules import (
    CONTEXT_AT,
    CONTEXT_LOCATION,
    NOTE_EXCERPT_CHARS,
    OPERATION_NAME_KEY,
    OPERATION_START_KEY,
    OPERATION_STATUS_DONE,
    OPERATION_STATUS_KEY,
    OPERATION_TYPE_KEY,
    PROJECT_READ_OPERATIONS,
    SECONDS_PER_DAY,
    new_result,
    one_line,
    operation_target,
    parse_rfc3339,
    project_read,
)

RULE_ID = "control-plane-upgrade-history"
ENTRY = 23
CAN_BLOCK = False

# How far back the operations are read: GKE lists a project's operations for a bounded time,
# and a failure older than this says little about the next window.
LOOKBACK_DAYS = 90
LOOKBACK_SECONDS = LOOKBACK_DAYS * SECONDS_PER_DAY
OPERATION_TYPES = ("UPGRADE_MASTER", "UPGRADE_NODES", "REPAIR_CLUSTER")
# What a capacity failure's message carries, matched without case: the Compute Engine
# phrasing, the stockout error codes, and the quota and resource shortfalls.
CAPACITY_MARKERS = ("does not have enough resources", "resource_pool_exhausted", "stockout", "insufficient")
# The operation record's error fields; the rest are the shared OPERATION_* keys.
OPERATION_ERROR_KEY = "error"
OPERATION_ERROR_MESSAGE_KEY = "message"
OPERATION_STATUS_MESSAGE_KEY = "statusMessage"
TYPE_ON_POOL_FORMAT = "{type} on {pool}"
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
# A daily window's `startTime` is HH:MM in UTC; a recurring window's is an RFC 3339 instant.
DAILY_START_RE = re.compile(r"^(\d{2}):(\d{2})$")
HOURS_PER_DAY = 24
MINUTES_PER_HOUR = 60
MINUTES_PER_DAY = HOURS_PER_DAY * MINUTES_PER_HOUR
TIME_FORMAT = "{hour:02d}:{minute:02d}"
# The region's busy hours in its standard local time, from BUSY_HOURS_START up to and not
# including BUSY_HOURS_END: the working day and the early evening, when the region's tenants
# draw its capacity. A window that opens at 21:00 or later, or before 06:00, is off-peak.
BUSY_HOURS_START = 6
BUSY_HOURS_END = 21
BUSY_HOURS_FORMAT = "{start:02d}:00 to {end:02d}:00"
BUSY_HOURS_TEXT = BUSY_HOURS_FORMAT.format(start=BUSY_HOURS_START, end=BUSY_HOURS_END)
# A location's region is the location without the zone letter a zonal location adds
# (us-central1-a: us-central1).
ZONE_SUFFIX_RE = re.compile(r"-[a-z]$")
# The standard UTC offset, in hours, of each region's city, from the provider's region list
# (https://cloud.google.com/compute/docs/regions-zones); a half-hour offset is a fraction. A
# region absent here leaves a window's opening unplaced (PLACEMENT_UNPLACED), never guessed.
REGION_UTC_OFFSET_HOURS = {
    "africa-south1": 2,  # Johannesburg
    "asia-east1": 8,  # Changhua County, Taiwan
    "asia-east2": 8,  # Hong Kong
    "asia-northeast1": 9,  # Tokyo
    "asia-northeast2": 9,  # Osaka
    "asia-northeast3": 9,  # Seoul
    "asia-south1": 5.5,  # Mumbai
    "asia-south2": 5.5,  # Delhi
    "asia-southeast1": 8,  # Jurong West, Singapore
    "asia-southeast2": 7,  # Jakarta
    "australia-southeast1": 10,  # Sydney
    "australia-southeast2": 10,  # Melbourne
    "europe-central2": 1,  # Warsaw
    "europe-north1": 2,  # Hamina, Finland
    "europe-north2": 1,  # Stockholm
    "europe-southwest1": 1,  # Madrid
    "europe-west1": 1,  # St. Ghislain, Belgium
    "europe-west2": 0,  # London
    "europe-west3": 1,  # Frankfurt
    "europe-west4": 1,  # Eemshaven, Netherlands
    "europe-west6": 1,  # Zurich
    "europe-west8": 1,  # Milan
    "europe-west9": 1,  # Paris
    "europe-west10": 1,  # Berlin
    "europe-west12": 1,  # Turin
    "me-central1": 3,  # Doha
    "me-central2": 3,  # Dammam
    "me-west1": 2,  # Tel Aviv
    "northamerica-northeast1": -5,  # Montreal
    "northamerica-northeast2": -5,  # Toronto
    "northamerica-south1": -6,  # Queretaro, Mexico
    "southamerica-east1": -3,  # Osasco, Sao Paulo
    "southamerica-west1": -4,  # Santiago
    "us-central1": -6,  # Council Bluffs, Iowa
    "us-east1": -5,  # Moncks Corner, South Carolina
    "us-east4": -5,  # Ashburn, Virginia
    "us-east5": -5,  # Columbus, Ohio
    "us-south1": -6,  # Dallas
    "us-west1": -8,  # The Dalles, Oregon
    "us-west2": -8,  # Los Angeles
    "us-west3": -7,  # Salt Lake City
    "us-west4": -8,  # Las Vegas
}
# Where the window opens against the region's busy hours; the finding carries it as `placement`.
PLACEMENT_NONE = "none"  # no maintenance window
PLACEMENT_BUSY = "busy"
PLACEMENT_OFF_PEAK = "off-peak"
PLACEMENT_UNPLACED = "unplaced"
# The clause the finding prints after the window ("<window>, which <clause>").
PLACEMENT_BUSY_FORMAT = "opens at {opens} in {region} standard time, inside the busy hours {busy}"
PLACEMENT_OFF_PEAK_FORMAT = "opens at {opens} in {region} standard time, off-peak"
PLACEMENT_UNPLACED_FORMAT = "is not placed against the busy hours {busy} ({detail})"
NO_OFFSET_DETAIL = "no standard-time offset is known for region {region}"
START_UNREAD_DETAIL = "its start time {start!r} could not be read"
WINDOW_PLACED_FORMAT = "{window}, which {placement}"
NOTIFICATIONS_ACTION = "keep upgrade notifications consumed so a failed attempt is a message the same day"
SET_WINDOW_ACTION = "set a maintenance window in an off-peak hour for the region and retry the upgrade in it; " + NOTIFICATIONS_ACTION
RETRY_ACTION_FORMAT = "retry the upgrade in the maintenance window ({window}); " + NOTIFICATIONS_ACTION
RISK_FORMAT = "{type} on {date} failed on compute capacity in {location} ({message}); maintenance window: {window}; {action}"
NOTE_FINDING_FORMAT = "{type} on {date} failed on compute capacity in {location} ({message}); {action}"
NOTE_FORMAT = "{failures} capacity failure(s) among {operations} upgrade or repair operation(s) in the last {days} days; maintenance window {window}"
UNPLACED_WINDOW_REASON = "the maintenance window ({window}) is not placed against the busy hours {busy}: {detail}; its capacity failure(s) stay risks with the set-window action"
OPERATIONS_NOT_READ_REASON = "the operations were not read ({error}), so the upgrade history is unknown"
OPERATIONS_NOT_A_LIST = "the operations read returned {type}, not a list"


def region_of(location: str) -> str:
    """The region of a GKE location: a zonal location without its zone letter, a regional one as is."""
    return ZONE_SUFFIX_RE.sub("", location)


def read_window(cluster: dict) -> tuple:
    """(the maintenance window as the finding prints it, WINDOW_NONE without one; its start as
    the record spells it, None without one; the start's (hour, minute) in UTC, None when there
    is no window or the start cannot be read). A daily window's `startTime` is HH:MM in UTC; a
    recurring window's is the RFC 3339 instant of its first occurrence, in any offset."""
    window = ((cluster.get(MAINTENANCE_POLICY_KEY) or {}).get(WINDOW_KEY)) or {}
    daily = window.get(DAILY_WINDOW_KEY)
    if isinstance(daily, dict) and daily.get(WINDOW_START_KEY):
        start = str(daily.get(WINDOW_START_KEY))
        text = DAILY_WINDOW_FORMAT.format(start=start, duration=daily.get(WINDOW_DURATION_KEY) or "")
        m = DAILY_START_RE.match(start.strip())
        if m and int(m.group(1)) < HOURS_PER_DAY and int(m.group(2)) < MINUTES_PER_HOUR:
            return text, start, (int(m.group(1)), int(m.group(2)))
        return text, start, None
    recurring = window.get(RECURRING_WINDOW_KEY)
    if isinstance(recurring, dict) and recurring.get(RECURRENCE_KEY):
        start = (recurring.get(WINDOW_KEY) or {}).get(WINDOW_START_KEY)
        text = RECURRING_WINDOW_FORMAT.format(recurrence=recurring.get(RECURRENCE_KEY), start=start or "")
        opened = parse_rfc3339(start)
        return text, start, ((opened.hour, opened.minute) if opened is not None else None)
    return WINDOW_NONE, None, None


def local_time(start_utc: tuple, offset_hours) -> tuple:
    """(hour, minute) in a standard time `offset_hours` from UTC of a UTC (hour, minute)."""
    minutes = (start_utc[0] * MINUTES_PER_HOUR + start_utc[1] + round(offset_hours * MINUTES_PER_HOUR)) % MINUTES_PER_DAY
    return divmod(minutes, MINUTES_PER_HOUR)


def place_window(cluster: dict, region: str) -> dict:
    """The cluster's maintenance window against the region's busy hours: `window` (the text
    the finding prints), `placement` (PLACEMENT_*), `opens` (HH:MM in the region's standard
    time, None when not placed), `text` (the clause printed after the window, None without a
    window) and `detail` (why it was not placed, None otherwise)."""
    window, start, start_utc = read_window(cluster)
    placed = {"window": window, "placement": PLACEMENT_NONE, "opens": None, "text": None, "detail": None}
    if window == WINDOW_NONE:
        return placed
    offset = REGION_UTC_OFFSET_HOURS.get(region)
    if start_utc is None or offset is None:
        placed["detail"] = START_UNREAD_DETAIL.format(start=start) if start_utc is None else NO_OFFSET_DETAIL.format(region=region)
        placed["placement"] = PLACEMENT_UNPLACED
        placed["text"] = PLACEMENT_UNPLACED_FORMAT.format(busy=BUSY_HOURS_TEXT, detail=placed["detail"])
        return placed
    hour, minute = local_time(start_utc, offset)
    placed["opens"] = TIME_FORMAT.format(hour=hour, minute=minute)
    if BUSY_HOURS_START <= hour < BUSY_HOURS_END:
        placed["placement"] = PLACEMENT_BUSY
        placed["text"] = PLACEMENT_BUSY_FORMAT.format(opens=placed["opens"], region=region, busy=BUSY_HOURS_TEXT)
    else:
        placed["placement"] = PLACEMENT_OFF_PEAK
        placed["text"] = PLACEMENT_OFF_PEAK_FORMAT.format(opens=placed["opens"], region=region)
    return placed


def failure_message(operation: dict) -> str:
    error = operation.get(OPERATION_ERROR_KEY)
    message = error.get(OPERATION_ERROR_MESSAGE_KEY) if isinstance(error, dict) else None
    return str(message or operation.get(OPERATION_STATUS_MESSAGE_KEY) or "")


def names_capacity(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in CAPACITY_MARKERS)


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
    placed = place_window(cluster, region_of(location))
    window = placed["window"]
    if placed["placement"] == PLACEMENT_OFF_PEAK:
        action = RETRY_ACTION_FORMAT.format(window=WINDOW_PLACED_FORMAT.format(window=window, placement=placed["text"]))
    else:
        action = SET_WINDOW_ACTION
    at = context[CONTEXT_AT]
    considered = 0
    failure_notes = []
    for operation in operations:
        if not isinstance(operation, dict) or operation.get(OPERATION_TYPE_KEY) not in OPERATION_TYPES or operation.get(OPERATION_STATUS_KEY) != OPERATION_STATUS_DONE:
            continue
        mine, pool = operation_target(operation, name, location)
        if not mine:
            continue
        started = parse_rfc3339(operation.get(OPERATION_START_KEY))
        if started is None or (at - started).total_seconds() > LOOKBACK_SECONDS:
            continue
        considered += 1
        message = failure_message(operation)
        if not (message and names_capacity(message)):
            continue
        finding = {
            "operation": operation.get(OPERATION_NAME_KEY),
            "operation_type": operation.get(OPERATION_TYPE_KEY),
            "pool": pool,
            "date": str(operation.get(OPERATION_START_KEY))[:DATE_CHARS],
            "location": location,
            "message": one_line(message, NOTE_EXCERPT_CHARS),
            "window": window,
            "placement": placed["placement"],
            "window_opens": placed["opens"],
            "placement_text": placed["text"],
            "action": action,
        }
        if placed["placement"] == PLACEMENT_OFF_PEAK:
            failure_notes.append(describe(finding))
        else:
            out["risks"].append(finding)
    failures = len(out["risks"]) + len(failure_notes)
    if failures and placed["placement"] == PLACEMENT_UNPLACED:
        out["unknown"].append(UNPLACED_WINDOW_REASON.format(window=window, busy=BUSY_HOURS_TEXT, detail=placed["detail"]))
    out["notes"].append(NOTE_FORMAT.format(failures=failures, operations=considered, days=LOOKBACK_DAYS, window=window))
    out["notes"].extend(failure_notes)
    return out


def describe(finding: dict) -> str:
    kind = TYPE_ON_POOL_FORMAT.format(type=finding["operation_type"], pool=finding["pool"]) if finding.get("pool") else finding["operation_type"]
    if finding.get("placement") == PLACEMENT_OFF_PEAK:
        return NOTE_FINDING_FORMAT.format(type=kind, date=finding["date"], location=finding["location"], message=finding["message"], action=finding["action"])
    window = WINDOW_PLACED_FORMAT.format(window=finding["window"], placement=finding["placement_text"]) if finding.get("placement_text") else finding["window"]
    return RISK_FORMAT.format(type=kind, date=finding["date"], location=finding["location"], message=finding["message"], window=window, action=finding["action"])
