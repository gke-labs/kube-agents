#!/usr/bin/env python3
"""
fleet_upgrade_report.py — per-member GKE version table against a target version.

Enumerates every cluster in the target projects with `gcloud container clusters list`,
reads each control-plane and node-pool version, and compares them with a target: the
`--target-version` given on the command line, or, without one, each cluster's own
release-channel `defaultVersion` from `gcloud container get-server-config`. Prints a
Markdown table on stdout and, with `--output`, writes the same data as JSON.

Each run also records its per-member result under a state directory, keyed by target,
and reads the previous run's record for the same target: a "Rollout progress" section
after the table says per member whether it `started`, `completed`, or is `unchanged`
since that run, and flags an unchanged, behind member as `stalled` while a rollout is
active (another member moved, or `--rollout-in-progress` was passed).

Read-only against GCP: the three gcloud commands it runs are `container clusters list`,
`container get-server-config`, and `config get-value project`. The only thing it writes
is its own state file and the optional `--output` JSON.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

# Project resolution, in the order networking_audit.py and the fleet SOPs use it:
# explicit --project flags, then the fleet's monitored-project list, then the
# per-profile project variables, then gcloud's configured default.
MONITORED_PROJECTS_ENV = "MONITORED_PROJECT_IDS"
PROJECT_ENV_VARS = ("GCP_PROJECT_ID", "GKE_PROJECT_ID", "PROJECT_ID")
GCLOUD = "gcloud"
JSON_FORMAT_FLAG = "--format=json"
# A stalled API call is reported as a failed read for its project or location rather
# than blocking the agent turn; the same budget compute_fleet_audit.py gives gcloud.
GCLOUD_TIMEOUT_SECONDS = 60

# `MAJOR.MINOR.PATCH-gke.BUILD`; the `-gke.BUILD` suffix is optional, and a version
# without it gets BUILD 0, as the security-patch-orchestrator SOP's comparison rule
# says. Anything else is unparsable and degrades its row to `unknown`.
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-gke\.(\d+))?$")

# Per-member verdicts. `lagging` is a minor (or major) behind the target on the control
# plane or a pool; `patch-behind` is the same minor with a lower patch or gke build,
# kept apart because a new patch reaches a channel default before any rollout wave has
# applied it, so a whole fleet is routinely patch-behind the morning after. `ahead` is
# reported and never flagged for the mirror-image reason. The SOP grades the same split
# as major versus minor severity.
STATUS_LAGGING = "lagging"
STATUS_PATCH_BEHIND = "patch-behind"
STATUS_CURRENT = "current"
STATUS_AHEAD = "ahead"
STATUS_UNKNOWN = "unknown"
STATUS_ORDER = (STATUS_LAGGING, STATUS_PATCH_BEHIND, STATUS_CURRENT, STATUS_AHEAD, STATUS_UNKNOWN)

# `releaseChannel.channel` values that mean "no channel"; such a member has no
# channel default to measure against and needs an explicit --target-version.
NO_CHANNEL_VALUES = ("", "UNSPECIFIED")

# How the target column labels a target that came from the channel rather than the
# flag, so the baseline is visible per member on a mixed-channel fleet.
TARGET_SOURCE_FLAG = "--target-version"
CHANNEL_DEFAULT_LABEL = "channel default ({channel})"

# Cluster and node-pool `status` values that mean an upgrade is in flight. The row is
# still graded (the plan's four states are the contract), but the note says so, as
# the SOP does before it suppresses a version finding.
IN_FLIGHT_STATUSES = ("RECONCILING", "PROVISIONING")

# Table rendering: the empty-cell placeholder, the header separator cell, the
# separator between the reasons a row's note carries, and the name shown for a pool
# record that has none.
EMPTY_CELL = "-"
TABLE_SEPARATOR_CELL = "---"
NOTE_SEPARATOR = "; "
UNNAMED_POOL = "?"
JSON_INDENT = 2
TABLE_COLUMNS = (
    "project",
    "cluster",
    "location",
    "channel",
    "control plane",
    "lowest node pool",
    "target",
    "gap (minors)",
    "status",
    "note",
)

# Where a run leaves its record for the next one. The script runs in the shell sandbox,
# whose /opt/data is a PersistentVolumeClaim the operator retains when the sandbox
# StatefulSet goes (shell_sandbox_manifests.go), and whose entrypoint replaces only the
# /opt/defaults trees and the database stubs on start (deploy/sandbox/entrypoint.sh);
# /opt/data/scratch is per-run scratch by convention, so the record goes in a directory
# of the skill's own name that nothing else writes. One file per target, named after
# it: the target string is validated by VERSION_RE, so it is filesystem-safe as is, and
# a run without --target-version measures each member against its channel default and
# keys its record under CHANNEL_DEFAULT_STATE_KEY. --state-dir overrides the directory.
DEFAULT_STATE_DIR = "/opt/data/state/fleet-upgrade-verification"
CHANNEL_DEFAULT_STATE_KEY = "channel-default"
STATE_FILE_SUFFIX = ".json"
# The record is written to a uniquely named sibling and renamed into place, so two
# runs against one target at once (a chat turn and a delegated card) cannot truncate
# each other's half-written file; the last rename wins whole.
STATE_TMP_PREFIX = ".fleet-upgrade-record-"
# Bumped when the record's shape changes; a file with another version is reported and
# treated as no prior run rather than compared field by field.
STATE_FORMAT_VERSION = 1
# `project/location/cluster`, the same triple the table sorts on; unique across a
# fleet because a project's cluster names are unique per location.
MEMBER_KEY_SEPARATOR = "/"
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Per-member progress since the previous run for the same target. `completed`: the
# member is current or ahead after a version change (a current member that moved to a
# new channel default included), or its status became current or ahead. `started`:
# the control plane or the lowest pool changed version without reaching the target, or
# the member is in flight. `unchanged`:
# the same versions and status as before. `new`: no prior record. `stalled` is an
# unchanged member that is lagging or patch-behind while a rollout is active, which is
# the signal a point-in-time table cannot give: a member that has not moved between two
# polls while its peers did.
PROGRESS_NEW = "new"
PROGRESS_STARTED = "started"
PROGRESS_COMPLETED = "completed"
PROGRESS_UNCHANGED = "unchanged"
PROGRESS_STALLED = "stalled"
PROGRESS_ORDER = (PROGRESS_COMPLETED, PROGRESS_STARTED, PROGRESS_STALLED, PROGRESS_UNCHANGED, PROGRESS_NEW)
# Statuses that mean the member has reached the target, and the ones a stall can be
# called on. `unknown` is in neither: a row that could not be graded is not a stall,
# and an `unknown` observation on either side of a comparison is not evidence of a
# move either (a timed-out get-server-config must not read as a completed upgrade),
# so the comparison falls back to the versions alone and the record keeps the last
# graded status.
DONE_STATUSES = (STATUS_CURRENT, STATUS_AHEAD)
BEHIND_STATUSES = (STATUS_LAGGING, STATUS_PATCH_BEHIND)
GRADED_STATUSES = DONE_STATUSES + BEHIND_STATUSES
# Why a rollout counted as active, for the summary line and the JSON.
ACTIVE_REASON_FLAG = "--rollout-in-progress"
ACTIVE_REASON_MOVERS = "another member moved since the previous run"
# Why a member in the previous record has no row this run.
GONE_REASON_ABSENT = "not in this run's cluster list; dropped from the record"
GONE_REASON_NOT_READ = "not read this run ({why}); carried forward"
NOT_READ_FAILED = "clusters list failed for {project}"
NOT_READ_OUT_OF_SCOPE = "{project} not in this run's projects"

# Elapsed-time rendering for `stalled (unchanged for ...)`.
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400
ELAPSED_UNDER_A_MINUTE = "<1m"
PROGRESS_COLUMNS = (
    "project",
    "cluster",
    "location",
    "previous (control plane / lowest pool)",
    "now (control plane / lowest pool)",
    "status",
    "progress",
)
VERSION_PAIR_SEPARATOR = " / "

# Exit codes. A failed gcloud call is reported per project and per location and does
# not abort the run; the exit code only says whether every requested read succeeded.
EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2


def run_cmd(cmd: list[str], timeout: int = GCLOUD_TIMEOUT_SECONDS) -> tuple[int, str, str]:
    """Runs a command and returns (rc, stdout, stderr); never raises."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
        return res.returncode, res.stdout, res.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out after {timeout} seconds"
    except Exception as e:  # noqa: BLE001 - a missing binary is a report, not a crash
        return -1, "", str(e)


def run_gcloud_json(cmd: list[str]) -> tuple[list | dict | None, str | None]:
    """Runs a gcloud command with JSON output and returns (parsed, error_message)."""
    rc, stdout, stderr = run_cmd(cmd)
    if rc != 0:
        return None, f"{' '.join(cmd)} failed ({rc}): {stderr.strip()}"
    if not stdout.strip():
        return [], None
    try:
        return json.loads(stdout), None
    except ValueError as e:
        return None, f"{' '.join(cmd)} returned unparsable JSON: {e}"


def get_target_projects(cli_projects: list[str] | None = None) -> list[str]:
    """Resolves the projects to enumerate; --project wins, then env, then gcloud."""
    if cli_projects:
        return sorted(set(p.strip() for p in cli_projects if p.strip()))

    projects = set()
    for p in os.environ.get(MONITORED_PROJECTS_ENV, "").split(","):
        p = p.strip()
        if p:
            projects.add(p)
    for env_var in PROJECT_ENV_VARS:
        val = os.environ.get(env_var, "").strip()
        if val:
            projects.add(val)
    if not projects:
        rc, stdout, _ = run_cmd([GCLOUD, "config", "get-value", "project"])
        if rc == 0 and stdout.strip():
            projects.add(stdout.strip())
    return sorted(projects)


def parse_version(text: str | None) -> tuple[int, int, int, int] | None:
    """`1.30.5-gke.1355000` -> (1, 30, 5, 1355000); None when it does not parse."""
    if not isinstance(text, str):
        return None
    m = VERSION_RE.match(text.strip())
    if not m:
        return None
    major, minor, patch, build = m.groups()
    return int(major), int(minor), int(patch), int(build or 0)


def minor_gap(target: tuple[int, int, int, int], member: tuple[int, int, int, int]) -> int | None:
    """Minors the member trails the target by; negative when ahead, None across majors."""
    if target[0] != member[0]:
        return None
    return target[1] - member[1]


def compare(target: tuple[int, int, int, int], member: tuple[int, int, int, int]) -> str:
    """One component's verdict against the target."""
    if member < target:
        return STATUS_LAGGING
    if member > target:
        return STATUS_AHEAD
    return STATUS_CURRENT


def lowest_node_pool(node_pools: list[dict]) -> tuple[dict | None, str | None]:
    """The pool with the lowest parsable version, and a note naming any pool skipped.

    A pool whose version does not parse is skipped and named, not allowed to hide a real
    lag on the pools that do parse; the result is None only when no pool parses.
    """
    if not isinstance(node_pools, list) or not node_pools:
        return None, "no nodePools in the cluster record"
    parsed = []
    skipped = []
    for pool in node_pools:
        if not isinstance(pool, dict):
            skipped.append(UNNAMED_POOL)
            continue
        version = parse_version(pool.get("version"))
        if version is None:
            skipped.append(f"{pool.get('name') or UNNAMED_POOL} ({pool.get('version')!r})")
            continue
        parsed.append((version, pool))
    note = f"node pool version unparsable, skipped: {', '.join(skipped)}" if skipped else None
    if not parsed:
        return None, note
    version, pool = min(parsed, key=lambda item: item[0])
    return {"name": pool.get("name", ""), "version": pool.get("version"), "status": pool.get("status", "")}, note


class ServerConfigCache:
    """`get-server-config` once per (project, location), as the SOP asks."""

    def __init__(self):
        self._cache: dict[tuple[str, str], tuple[dict | None, str | None]] = {}
        self.errors: list[dict] = []

    def get(self, project: str, location: str) -> tuple[dict | None, str | None]:
        key = (project, location)
        if key not in self._cache:
            cmd = [GCLOUD, "container", "get-server-config", f"--location={location}", f"--project={project}", JSON_FORMAT_FLAG]
            data, error = run_gcloud_json(cmd)
            if error is None and not isinstance(data, dict):
                data, error = None, f"{' '.join(cmd)} returned no server config"
            if error is not None:
                self.errors.append({"project": project, "location": location, "message": error})
            self._cache[key] = (data, error)
        return self._cache[key]


def channel_default(server_config: dict, channel: str) -> str | None:
    """The `defaultVersion` of the named channel in a server config, or None."""
    for entry in server_config.get("channels", []) or []:
        if isinstance(entry, dict) and entry.get("channel") == channel:
            return entry.get("defaultVersion")
    return None


def resolve_target(cluster: dict, project: str, explicit_target: str | None, cache: ServerConfigCache) -> tuple[str | None, str, str | None]:
    """(target_version, target_source, reason_when_missing) for one member."""
    if explicit_target:
        return explicit_target, TARGET_SOURCE_FLAG, None
    channel = (cluster.get("releaseChannel") or {}).get("channel", "") or ""
    if channel in NO_CHANNEL_VALUES:
        return None, EMPTY_CELL, "no release channel; pass --target-version"
    location = cluster.get("location", "")
    server_config, error = cache.get(project, location)
    if server_config is None:
        return None, CHANNEL_DEFAULT_LABEL.format(channel=channel), f"get-server-config failed for {location}"
    default = channel_default(server_config, channel)
    if not default:
        return None, CHANNEL_DEFAULT_LABEL.format(channel=channel), f"channel {channel} not in get-server-config for {location}"
    return default, CHANNEL_DEFAULT_LABEL.format(channel=channel), None


def grade_member(cluster: dict, project: str, explicit_target: str | None, cache: ServerConfigCache) -> dict:
    """One row: versions read, target chosen, and the verdict."""
    channel = (cluster.get("releaseChannel") or {}).get("channel", "") or ""
    master_text = cluster.get("currentMasterVersion")
    node_pools = cluster.get("nodePools") or []
    lowest, pool_reason = lowest_node_pool(node_pools)
    target_text, target_source, target_reason = resolve_target(cluster, project, explicit_target, cache)

    member = {
        "project": project,
        "cluster": cluster.get("name", ""),
        "location": cluster.get("location", ""),
        "channel": channel or None,
        "cluster_status": cluster.get("status", ""),
        "control_plane_version": master_text,
        "node_pools": [
            {"name": p.get("name", ""), "version": p.get("version"), "status": p.get("status", "")}
            for p in node_pools
            if isinstance(p, dict)
        ],
        "lowest_node_pool": lowest,
        "target_version": target_text,
        "target_source": target_source,
        "gap_minors": None,
        "status": STATUS_UNKNOWN,
        "note": "",
    }

    notes = []
    master = parse_version(master_text)
    target = parse_version(target_text) if target_text else None
    if master is None:
        notes.append(f"control plane version unparsable: {master_text!r}")
    if pool_reason:
        notes.append(pool_reason)
    if target_text is None:
        notes.append(target_reason)
    elif target is None:
        notes.append(f"target version unparsable: {target_text!r}")

    in_flight = [cluster.get("status", "")] + [p.get("status", "") for p in member["node_pools"]]
    if any(s in IN_FLIGHT_STATUSES for s in in_flight):
        notes.append("upgrade in flight (RECONCILING/PROVISIONING)")

    if master is not None and lowest is not None and target is not None:
        pool_version = parse_version(lowest["version"])
        lowest_component = min(master, pool_version)
        verdicts = {compare(target, master), compare(target, pool_version)}
        member["gap_minors"] = minor_gap(target, lowest_component)
        if STATUS_LAGGING in verdicts:
            # Below the target somewhere: a minor or major behind is lagging; the same
            # minor with a lower patch or build is patch-behind.
            member["status"] = STATUS_LAGGING if member["gap_minors"] != 0 else STATUS_PATCH_BEHIND
        elif verdicts == {STATUS_CURRENT}:
            member["status"] = STATUS_CURRENT
        else:
            member["status"] = STATUS_AHEAD
        if member["gap_minors"] is None:
            notes.append("major version differs from the target; minor gap undefined")

    member["note"] = NOTE_SEPARATOR.join(n for n in notes if n)
    return member


def build_report(projects: list[str], explicit_target: str | None) -> dict:
    """Enumerates every project and grades every member; one failure never aborts the rest."""
    cache = ServerConfigCache()
    members: list[dict] = []
    errors: list[dict] = []
    for project in projects:
        cmd = [GCLOUD, "container", "clusters", "list", f"--project={project}", JSON_FORMAT_FLAG]
        clusters, error = run_gcloud_json(cmd)
        if error is not None or not isinstance(clusters, list):
            errors.append({"project": project, "location": None, "message": error or f"{' '.join(cmd)} returned no list"})
            continue
        for cluster in clusters:
            if isinstance(cluster, dict):
                members.append(grade_member(cluster, project, explicit_target, cache))
    errors.extend(cache.errors)
    members.sort(key=lambda m: (m["project"], m["location"], m["cluster"]))
    return {
        "target_version": explicit_target,
        "projects": list(projects),
        "members": members,
        "errors": errors,
        "summary": {status: sum(1 for m in members if m["status"] == status) for status in STATUS_ORDER},
    }


def _cell(value) -> str:
    if value is None or value == "":
        return EMPTY_CELL
    return str(value).replace("|", "\\|")


def render_table(report: dict) -> str:
    """The Markdown table plus a summary line and any errors, for the chat reply."""
    lines = [
        "| " + " | ".join(TABLE_COLUMNS) + " |",
        "| " + " | ".join(TABLE_SEPARATOR_CELL for _ in TABLE_COLUMNS) + " |",
    ]
    for m in report["members"]:
        lowest = m["lowest_node_pool"]
        lowest_cell = f"{lowest['version']} ({lowest['name']})" if lowest else None
        target_cell = None
        if m["target_version"]:
            target_cell = m["target_version"]
            if m["target_source"] != TARGET_SOURCE_FLAG:
                target_cell = f"{m['target_version']} {m['target_source']}"
        elif m["target_source"] != EMPTY_CELL:
            target_cell = m["target_source"]
        row = (
            m["project"],
            m["cluster"],
            m["location"],
            m["channel"],
            m["control_plane_version"],
            lowest_cell,
            target_cell,
            m["gap_minors"],
            m["status"],
            m["note"],
        )
        lines.append("| " + " | ".join(_cell(v) for v in row) + " |")
    summary = report["summary"]
    lines.append("")
    lines.append(
        f"{len(report['members'])} member(s) across {len(report['projects'])} project(s): "
        + ", ".join(f"{summary[s]} {s}" for s in STATUS_ORDER)
        + (f"; target {report['target_version']}" if report["target_version"] else "; target: each cluster's channel default")
    )
    for err in report["errors"]:
        where = err["project"] + (f" ({err['location']})" if err.get("location") else "")
        lines.append(f"- read failed for {where}: {err['message']}")
    return "\n".join(lines)

def utc_now() -> datetime:
    """The clock every timestamp in the state file comes from; tests patch it."""
    return datetime.now(timezone.utc)


def format_timestamp(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def parse_timestamp(text) -> datetime | None:
    """A TIMESTAMP_FORMAT string back to an aware datetime; None when it does not parse."""
    if not isinstance(text, str):
        return None
    try:
        return datetime.strptime(text, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def format_elapsed(seconds: int) -> str:
    """`2h 15m`, `3d 1h`, `45m`; under a minute is ELAPSED_UNDER_A_MINUTE."""
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, SECONDS_PER_DAY)
    hours, rest = divmod(rest, SECONDS_PER_HOUR)
    minutes = rest // SECONDS_PER_MINUTE
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return ELAPSED_UNDER_A_MINUTE


def member_key(member: dict) -> str:
    return MEMBER_KEY_SEPARATOR.join((member["project"], member["location"], member["cluster"]))


def state_path(state_dir: str, explicit_target: str | None) -> str:
    """The record for this target: `<state_dir>/<target>.json`, or the channel-default file."""
    return os.path.join(state_dir, (explicit_target or CHANNEL_DEFAULT_STATE_KEY) + STATE_FILE_SUFFIX)


def load_state(path: str) -> tuple[dict | None, str | None]:
    """(previous record, error). No file is (None, None); a bad file is (None, why)."""
    if not os.path.exists(path):
        return None, None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return None, f"previous record {path} unreadable, starting a new baseline: {e}"
    if not isinstance(data, dict) or data.get("format_version") != STATE_FORMAT_VERSION or not isinstance(data.get("members"), dict):
        return None, f"previous record {path} is not a format-{STATE_FORMAT_VERSION} record, starting a new baseline"
    return data, None


def save_state(path: str, state: dict) -> None:
    """Writes the record atomically (unique temp file, then rename); raises OSError on failure."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=STATE_TMP_PREFIX, suffix=STATE_FILE_SUFFIX, dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=JSON_INDENT)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _member_record(member: dict, unchanged_since: str, previous: dict | None) -> dict:
    """The member's entry in the record; an ungraded run keeps the last graded status."""
    lowest = member["lowest_node_pool"]
    status = member["status"]
    if status == STATUS_UNKNOWN and previous and previous.get("status") in GRADED_STATUSES:
        status = previous["status"]
    return {
        "control_plane_version": member["control_plane_version"],
        "lowest_node_pool_version": lowest["version"] if lowest else None,
        "status": status,
        "unchanged_since": unchanged_since,
    }


def _delta(member: dict, previous: dict | None) -> tuple[str, bool]:
    """(progress before the stall rule, whether this observation agrees with the previous)."""
    if previous is None:
        return PROGRESS_NEW, False
    lowest = member["lowest_node_pool"]
    versions_now = (member["control_plane_version"], lowest["version"] if lowest else None)
    versions_before = (previous.get("control_plane_version"), previous.get("lowest_node_pool_version"))
    status_now, status_before = member["status"], previous.get("status")
    moved = versions_now != versions_before
    # `completed` needs evidence of a move: a version change, or a graded behind status
    # before. A member that was already at the target and changed version is at the
    # target again (the channel default moved and it followed), which is a completed
    # upgrade, not a started one. An `unknown` baseline (a failed server-config read)
    # proves nothing on its own.
    if status_now in DONE_STATUSES and (moved or status_before in BEHIND_STATUSES):
        return PROGRESS_COMPLETED, False
    in_flight = [member["cluster_status"]] + [p.get("status", "") for p in member["node_pools"]]
    if moved or any(s in IN_FLIGHT_STATUSES for s in in_flight):
        return PROGRESS_STARTED, False
    # Same versions. A graded status that moved without them (the channel default
    # advanced under a member) starts a new observation series rather than inheriting
    # the old one's clock, so a stall is never dated from before the target it is
    # measured against existed. An `unknown` on either side is not a status change:
    # the versions agree, and that is all the observation says.
    both_graded = status_now in GRADED_STATUSES and status_before in GRADED_STATUSES
    return PROGRESS_UNCHANGED, status_now == status_before or not both_graded


def compute_progress(report: dict, previous: dict | None, now: datetime, rollout_flag: bool, path: str) -> dict:
    """Annotates every member with its progress since `previous`; returns the record to save.

    A member's `progress` is one of PROGRESS_ORDER; `unchanged_since` is the timestamp of
    the first of the consecutive agreeing observations, carried forward across runs, and
    `unchanged_for_seconds` the time since it. `report["rollout"]` gets the run-level view:
    whether the rollout counted as active and why, the per-progress counts, and the members
    of the previous record that have no row this run.
    """
    now_text = format_timestamp(now)
    prior_members = previous["members"] if previous else {}
    failed_projects = {e["project"] for e in report["errors"] if e.get("location") is None}
    read_projects = set(report["projects"]) - failed_projects

    record: dict[str, dict] = {}
    agreeing: set[str] = set()
    for member in report["members"]:
        key = member_key(member)
        before = prior_members.get(key)
        progress, agrees = _delta(member, before if isinstance(before, dict) else None)
        since = now_text
        if agrees:
            since = before.get("unchanged_since") or (previous or {}).get("recorded_at") or now_text
            agreeing.add(key)
        member["progress"] = progress
        member["unchanged_since"] = since
        since_at = parse_timestamp(since)
        member["unchanged_for_seconds"] = int((now - since_at).total_seconds()) if since_at else None
        record[key] = _member_record(member, since, before if isinstance(before, dict) else None)

    movers = [m for m in report["members"] if m["progress"] in (PROGRESS_STARTED, PROGRESS_COMPLETED)]
    active_reason = None
    if rollout_flag:
        active_reason = ACTIVE_REASON_FLAG
    elif movers:
        active_reason = ACTIVE_REASON_MOVERS
    if active_reason:
        for member in report["members"]:
            if member["progress"] == PROGRESS_UNCHANGED and member["status"] in BEHIND_STATUSES and member_key(member) in agreeing:
                member["progress"] = PROGRESS_STALLED

    # Members of the previous record with no row this run: gone when their project was
    # read cleanly (a deleted cluster leaves the record); carried forward when it was not
    # read at all, so a failed or narrowed read never loses a record or dates a stall
    # from it.
    gone = []
    for key, before in prior_members.items():
        if key in record or not isinstance(before, dict):
            continue
        project = key.split(MEMBER_KEY_SEPARATOR, 1)[0]
        if project in read_projects:
            gone.append({"member": key, "reason": GONE_REASON_ABSENT, "carried_forward": False})
            continue
        why = NOT_READ_FAILED.format(project=project) if project in failed_projects else NOT_READ_OUT_OF_SCOPE.format(project=project)
        gone.append({"member": key, "reason": GONE_REASON_NOT_READ.format(why=why), "carried_forward": True})
        record[key] = before

    report["rollout"] = {
        "state_file": path,
        "recorded_at": now_text,
        "previous_run_at": previous.get("recorded_at") if previous else None,
        "active": active_reason is not None,
        "active_reason": active_reason,
        "summary": {p: sum(1 for m in report["members"] if m["progress"] == p) for p in PROGRESS_ORDER},
        "missing_members": gone,
    }
    return {
        "format_version": STATE_FORMAT_VERSION,
        "target": report["target_version"],
        "recorded_at": now_text,
        "members": dict(sorted(record.items())),
    }


def _versions_cell(control_plane, lowest_pool) -> str:
    return _cell(control_plane) + VERSION_PAIR_SEPARATOR + _cell(lowest_pool)


def render_progress(report: dict, previous: dict | None) -> str:
    """The "Rollout progress" section printed after the table; one line on a first run."""
    rollout = report["rollout"]
    target = f"target {report['target_version']}" if report["target_version"] else "each cluster's channel default"
    if previous is None:
        return f"Rollout progress: no previous run for {target}; baseline recorded at {rollout['state_file']}. Rerun after the next wave to see per-member deltas."
    lines = [
        f"Rollout progress against {target}, compared with the run at {rollout['previous_run_at']} (record: {rollout['state_file']}):",
        "",
        "| " + " | ".join(PROGRESS_COLUMNS) + " |",
        "| " + " | ".join(TABLE_SEPARATOR_CELL for _ in PROGRESS_COLUMNS) + " |",
    ]
    prior_members = previous["members"]
    for m in report["members"]:
        before = prior_members.get(member_key(m))
        before = before if isinstance(before, dict) else {}
        lowest = m["lowest_node_pool"]
        progress = m["progress"]
        if progress == PROGRESS_STALLED:
            progress = f"{PROGRESS_STALLED} (unchanged for {format_elapsed(m['unchanged_for_seconds'] or 0)})"
        row = (
            m["project"],
            m["cluster"],
            m["location"],
            _versions_cell(before.get("control_plane_version"), before.get("lowest_node_pool_version")) if before else EMPTY_CELL,
            _versions_cell(m["control_plane_version"], lowest["version"] if lowest else None),
            m["status"],
            progress,
        )
        lines.append("| " + " | ".join(_cell(v) for v in row) + " |")
    summary = rollout["summary"]
    active = f"rollout active ({rollout['active_reason']})" if rollout["active"] else "no member moved and --rollout-in-progress not passed, so nothing is flagged stalled"
    lines.append("")
    lines.append(f"{len(report['members'])} member(s): " + ", ".join(f"{summary[p]} {p}" for p in PROGRESS_ORDER) + f"; {active}.")
    for gone in rollout["missing_members"]:
        lines.append(f"- {gone['member']}: in the previous record, {gone['reason']}")
    return "\n".join(lines)



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Per-member GKE version table against a target version.")
    parser.add_argument("--project", action="append", help="GCP project to enumerate; repeatable. Defaults to the fleet's configured projects.")
    parser.add_argument("--target-version", help="Target for every member, e.g. 1.31.4-gke.1183000. Default: each cluster's channel defaultVersion.")
    parser.add_argument("--output", help="Path to write the report as JSON.")
    parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help=f"Directory holding one record per target from the previous run (default: {DEFAULT_STATE_DIR}).")
    parser.add_argument("--rollout-in-progress", action="store_true", help="Assert a rollout is under way, so an unchanged, behind member is flagged stalled even when no other member moved.")
    args = parser.parse_args(argv)

    if args.target_version and parse_version(args.target_version) is None:
        sys.stderr.write(f"--target-version {args.target_version!r} is not MAJOR.MINOR.PATCH[-gke.BUILD]\n")
        return EXIT_USAGE

    projects = get_target_projects(args.project)
    if not projects:
        sys.stderr.write("no project: pass --project, or set MONITORED_PROJECT_IDS or GCP_PROJECT_ID\n")
        return EXIT_USAGE

    report = build_report(projects, args.target_version)
    path = state_path(args.state_dir, args.target_version)
    previous, state_error = load_state(path)
    state = compute_progress(report, previous, utc_now(), args.rollout_in_progress, path)
    print(render_table(report))
    print()
    print(render_progress(report, previous))
    if state_error:
        sys.stderr.write(state_error + "\n")

    write_failed = False
    try:
        save_state(path, state)
    except OSError as e:
        sys.stderr.write(f"failed to write the record {path}: {e}\n")
        write_failed = True

    if args.output:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=JSON_INDENT)
            print(f"\nWrote {len(report['members'])} member(s) to {args.output}")
        except OSError as e:
            sys.stderr.write(f"failed to write {args.output}: {e}\n")
            write_failed = True

    return EXIT_PARTIAL if report["errors"] or write_failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
