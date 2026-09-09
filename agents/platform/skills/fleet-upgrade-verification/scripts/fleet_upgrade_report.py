#!/usr/bin/env python3
"""
fleet_upgrade_report.py — per-member GKE version table against a target version.

Enumerates every cluster in the target projects with `gcloud container clusters list`,
reads each control-plane and node-pool version, and compares them with a target: the
`--target-version` given on the command line, or, without one, each cluster's own
release-channel `defaultVersion` from `gcloud container get-server-config`. Prints a
Markdown table on stdout and, with `--output`, writes the same data as JSON.

Read-only: the three gcloud commands it runs are `container clusters list`,
`container get-server-config`, and `config get-value project`.
"""

import argparse
import json
import os
import re
import subprocess
import sys

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Per-member GKE version table against a target version.")
    parser.add_argument("--project", action="append", help="GCP project to enumerate; repeatable. Defaults to the fleet's configured projects.")
    parser.add_argument("--target-version", help="Target for every member, e.g. 1.31.4-gke.1183000. Default: each cluster's channel defaultVersion.")
    parser.add_argument("--output", help="Path to write the report as JSON.")
    args = parser.parse_args(argv)

    if args.target_version and parse_version(args.target_version) is None:
        sys.stderr.write(f"--target-version {args.target_version!r} is not MAJOR.MINOR.PATCH[-gke.BUILD]\n")
        return EXIT_USAGE

    projects = get_target_projects(args.project)
    if not projects:
        sys.stderr.write("no project: pass --project, or set MONITORED_PROJECT_IDS or GCP_PROJECT_ID\n")
        return EXIT_USAGE

    report = build_report(projects, args.target_version)
    print(render_table(report))

    if args.output:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=JSON_INDENT)
            print(f"\nWrote {len(report['members'])} member(s) to {args.output}")
        except OSError as e:
            sys.stderr.write(f"failed to write {args.output}: {e}\n")
            return EXIT_PARTIAL

    return EXIT_PARTIAL if report["errors"] else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
